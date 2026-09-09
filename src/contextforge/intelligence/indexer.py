"""Structural-only CodeMap persistence and incremental reuse orchestration."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

from contextforge.context import ReaderLimits, read_selected_text_file
from contextforge.intelligence.codemap import (
    CODEMAP_SCHEMA_VERSION,
    RESOLVER_VERSION,
    FileCodeMap,
    deserialize_code_map,
    serialize_code_map,
)
from contextforge.intelligence.extractors import extract_code_map
from contextforge.intelligence.fallback import FALLBACK_ANALYZER
from contextforge.intelligence.graph import (
    OrientationMap,
    RelationshipGraph,
    build_orientation_map,
    build_relationship_graph,
)
from contextforge.intelligence.manifest import (
    build_index_manifest,
    calculate_source_snapshot_digest,
    canonical_json_bytes,
)
from contextforge.intelligence.models import (
    AnalyzerIdentity,
    ArtifactReference,
    GenerationArtifacts,
    IndexBuildState,
    IndexedFileState,
    IndexManifest,
    SchemaVersionMetadata,
    analyzer_identity_key,
)
from contextforge.intelligence.polyglot import (
    POLYGLOT_ANALYZER,
    SUPPORTED_POLYGLOT_LANGUAGES,
)
from contextforge.intelligence.python import (
    DEFAULT_CODEMAP_SOURCE_LIMIT,
    PYTHON_ANALYZER,
)
from contextforge.intelligence.relationships import resolve_relationships
from contextforge.intelligence.retrieval import build_retrieval_index
from contextforge.intelligence.store import (
    IndexManifestNotFoundError,
    IndexManifestReadError,
    IndexWriteLock,
    UnsupportedIndexSchemaError,
    load_generation_record,
    load_index_record,
    load_manifest,
    write_index_record,
    write_manifest,
)
from contextforge.repositories import ProjectFile, ProjectSnapshot


@dataclass(frozen=True, slots=True)
class StructuralIndexBuildResult:
    """Published structural result and explicit incremental-work accounting."""

    manifest: IndexManifest
    code_maps: tuple[FileCodeMap, ...]
    extracted_paths: tuple[str, ...]
    reused_paths: tuple[str, ...]
    generation_path: Path


def build_structural_index(
    snapshot: ProjectSnapshot,
    lock: IndexWriteLock,
    *,
    max_source_bytes: int = DEFAULT_CODEMAP_SOURCE_LIMIT,
    previous_manifest: IndexManifest | None = None,
    cancellation: asyncio.Event | None = None,
) -> StructuralIndexBuildResult:
    """Extract, resolve, and atomically persist facts without semantic analysis."""

    _validate_build_inputs(snapshot, lock, max_source_bytes)
    previous = (
        previous_manifest if previous_manifest is not None else _optional_manifest(lock)
    )
    options_digest = _build_options_digest(max_source_bytes)
    snapshot_digest = calculate_source_snapshot_digest(snapshot)
    previous_states = (
        {} if previous is None else {item.path: item for item in previous.files}
    )

    base_maps: list[FileCodeMap] = []
    extracted: list[str] = []
    reused: list[str] = []
    all_records_valid = previous is not None
    for project_file in sorted(snapshot.files, key=lambda item: item.path):
        _raise_if_cancelled(cancellation)
        state = previous_states.get(project_file.path)
        code_map = _reuse_code_map(lock, previous, state, project_file)
        if code_map is None:
            all_records_valid = False
            code_map = extract_code_map(
                snapshot,
                project_file,
                max_source_bytes=max_source_bytes,
            )
            extracted.append(project_file.path)
        else:
            _verify_reused_source(
                snapshot,
                project_file,
                max_source_bytes=max_source_bytes,
            )
            reused.append(project_file.path)
        base_maps.append(code_map)

    if previous is not None and (
        set(previous_states) != {item.path for item in snapshot.files}
        or previous.build.source_snapshot_digest != snapshot_digest
        or previous.build.build_options_digest != options_digest
        or previous.schema_versions != SchemaVersionMetadata()
    ):
        all_records_valid = False

    if all_records_valid and previous is not None:
        generation = lock.layout.generations / previous.generation_id
        return StructuralIndexBuildResult(
            manifest=previous,
            code_maps=tuple(base_maps),
            extracted_paths=(),
            reused_paths=tuple(reused),
            generation_path=generation,
        )

    _raise_if_cancelled(cancellation)
    code_maps = resolve_relationships(tuple(base_maps))
    states: list[IndexedFileState] = []
    record_digests: list[tuple[str, str]] = []
    for code_map in code_maps:
        _raise_if_cancelled(cancellation)
        content = serialize_code_map(code_map)
        location = _record_location(code_map.path)
        digest = write_index_record(lock, location, content)
        record_digests.append((code_map.path, digest))
        states.append(
            IndexedFileState(
                path=code_map.path,
                source_sha256=code_map.source_sha256,
                source_size_bytes=code_map.source_size_bytes,
                language=code_map.language,
                analyzer=code_map.analyzer,
                record_location=location,
                record_sha256=digest,
                record_status=(
                    "unsupported"
                    if code_map.parse_status == "unsupported"
                    else "complete"
                ),
            )
        )

    symbols_content = b"".join(
        canonical_json_bytes(symbol.model_dump(mode="json"))
        for symbol in sorted(
            (symbol for code_map in code_maps for symbol in code_map.symbols),
            key=lambda item: item.symbol_id,
        )
    )
    relationships_content = b"".join(
        canonical_json_bytes(relationship.model_dump(mode="json"))
        for relationship in sorted(
            (
                relationship
                for code_map in code_maps
                for relationship in code_map.relationships
            ),
            key=lambda item: item.relationship_id,
        )
    )
    symbols_digest = write_index_record(lock, "symbols.jsonl", symbols_content)
    relationships_digest = write_index_record(
        lock, "relationships.jsonl", relationships_content
    )
    graph = build_relationship_graph(code_maps, snapshot_digest)
    graph_content = canonical_json_bytes(graph.model_dump(mode="json"))
    graph_digest = write_index_record(lock, "relationship-graph.json", graph_content)
    orientation = build_orientation_map(code_maps, graph)
    orientation_content = canonical_json_bytes(orientation.model_dump(mode="json"))
    orientation_digest = write_index_record(
        lock, "orientation.json", orientation_content
    )
    structural_retrieval = build_retrieval_index(code_maps, (), snapshot_digest)
    structural_retrieval_content = canonical_json_bytes(
        structural_retrieval.model_dump(mode="json")
    )
    structural_retrieval_digest = write_index_record(
        lock, "retrieval-structural.json", structural_retrieval_content
    )
    facts_digest = hashlib.sha256(
        canonical_json_bytes(
            {
                "records": record_digests,
                "relationships": relationships_digest,
                "symbols": symbols_digest,
                "relationship_graph": graph_digest,
                "structural_retrieval": structural_retrieval_digest,
                "orientation": orientation_digest,
            }
        )
    ).hexdigest()
    analyzers = tuple(
        sorted({item.analyzer for item in code_maps}, key=analyzer_identity_key)
    )
    build = IndexBuildState(
        source_snapshot_digest=snapshot_digest,
        index_config_digest=_index_config_digest(),
        build_options_digest=options_digest,
        facts_digest=facts_digest,
        previous_generation_id=(
            previous.generation_id if previous is not None else None
        ),
    )
    _raise_if_cancelled(cancellation)
    manifest = build_index_manifest(
        build=build,
        files=states,
        structural_analyzers=analyzers,
        generation_kind="structural",
        artifacts=GenerationArtifacts(
            relationship_graph=ArtifactReference(
                location="relationship-graph.json", sha256=graph_digest
            ),
            structural_retrieval=ArtifactReference(
                location="retrieval-structural.json",
                sha256=structural_retrieval_digest,
            ),
            orientation_map=ArtifactReference(
                location="orientation.json", sha256=orientation_digest
            ),
        ),
    )
    generation = write_manifest(lock, manifest)
    return StructuralIndexBuildResult(
        manifest=manifest,
        code_maps=code_maps,
        extracted_paths=tuple(extracted),
        reused_paths=tuple(reused),
        generation_path=generation,
    )


def load_file_code_map(
    repository_root: str | Path,
    path: str,
    *,
    manifest: IndexManifest | None = None,
) -> FileCodeMap:
    """Load one digest-checked strict CodeMap from a caller-pinned generation."""

    active = manifest if manifest is not None else load_manifest(repository_root)
    state = next((item for item in active.files if item.path == path), None)
    if state is None:
        raise IndexManifestReadError("CodeMap path is absent from the pinned manifest")
    try:
        code_map = deserialize_code_map(
            load_index_record(repository_root, state, manifest=active)
        )
    except ValueError as exc:
        raise IndexManifestReadError(
            "published CodeMap does not match its schema"
        ) from exc
    if code_map.schema_version != active.schema_version or not _map_matches_state(
        code_map, state
    ):
        raise IndexManifestReadError(
            "CodeMap identity does not match its manifest state"
        )
    return code_map


def load_relationship_graph(
    repository_root: str | Path,
    *,
    manifest: IndexManifest | None = None,
) -> RelationshipGraph:
    """Load the digest-checked relationship graph from a pinned generation."""

    active = manifest if manifest is not None else load_manifest(repository_root)
    reference = active.artifacts.relationship_graph
    if reference is None:
        raise IndexManifestReadError("pinned generation has no relationship graph")
    try:
        graph = RelationshipGraph.model_validate_json(
            load_generation_record(repository_root, reference.location, manifest=active)
        )
    except ValueError as exc:
        raise IndexManifestReadError(
            "published relationship graph does not match its schema"
        ) from exc
    if graph.source_snapshot_digest != active.build.source_snapshot_digest:
        raise IndexManifestReadError("relationship graph is stale for its generation")
    return graph


def load_orientation_map(
    repository_root: str | Path,
    *,
    manifest: IndexManifest | None = None,
) -> OrientationMap:
    """Load the digest-checked all-file orientation map."""

    active = manifest if manifest is not None else load_manifest(repository_root)
    reference = active.artifacts.orientation_map
    if reference is None:
        raise IndexManifestReadError("pinned generation has no orientation map")
    try:
        orientation = OrientationMap.model_validate_json(
            load_generation_record(repository_root, reference.location, manifest=active)
        )
    except ValueError as exc:
        raise IndexManifestReadError(
            "published orientation map does not match its schema"
        ) from exc
    if orientation.source_snapshot_digest != active.build.source_snapshot_digest:
        raise IndexManifestReadError("orientation map is stale for its generation")
    return orientation


def _reuse_code_map(
    lock: IndexWriteLock,
    previous: IndexManifest | None,
    state: IndexedFileState | None,
    project_file: ProjectFile,
) -> FileCodeMap | None:
    expected_analyzer = _analyzer_for(project_file)
    if (
        previous is None
        or previous.schema_versions != SchemaVersionMetadata()
        or state is None
        or state.source_sha256 != project_file.sha256
        or state.source_size_bytes != project_file.size_bytes
        or state.language != project_file.language
        or state.analyzer != expected_analyzer
        or state.record_status not in {"complete", "unsupported"}
    ):
        return None
    try:
        code_map = deserialize_code_map(
            load_index_record(lock.layout.repository_root, state, manifest=previous)
        )
    except (ValueError, IndexManifestReadError):
        return None
    return (
        code_map
        if (
            code_map.schema_version == CODEMAP_SCHEMA_VERSION
            and _map_matches_state(code_map, state)
            and not any(item.code == "extractor_error" for item in code_map.diagnostics)
        )
        else None
    )


def _map_matches_state(code_map: FileCodeMap, state: IndexedFileState) -> bool:
    return (
        code_map.schema_version in {1, CODEMAP_SCHEMA_VERSION}
        and code_map.path == state.path
        and code_map.source_sha256 == state.source_sha256
        and code_map.source_size_bytes == state.source_size_bytes
        and code_map.language == state.language
        and code_map.analyzer == state.analyzer
    )


def _verify_reused_source(
    snapshot: ProjectSnapshot,
    project_file: ProjectFile,
    *,
    max_source_bytes: int,
) -> None:
    """Reauthorize and hash-check reuse without repeating structural parsing."""

    read_selected_text_file(
        snapshot,
        project_file,
        limits=ReaderLimits(
            max_files=1,
            max_source_bytes=max_source_bytes,
            max_content_bytes=max_source_bytes,
        ),
    )


def _validate_build_inputs(
    snapshot: ProjectSnapshot, lock: IndexWriteLock, max_source_bytes: int
) -> None:
    if not isinstance(snapshot, ProjectSnapshot):
        raise ValueError("expected a ProjectSnapshot")
    if type(max_source_bytes) is not int or max_source_bytes <= 0:
        raise ValueError("max_source_bytes must be a positive integer")
    if snapshot.root != lock.layout.repository_root:
        raise ValueError("snapshot root does not match the locked repository")


def _raise_if_cancelled(cancellation: asyncio.Event | None) -> None:
    if cancellation is not None and cancellation.is_set():
        raise asyncio.CancelledError


def _optional_manifest(lock: IndexWriteLock) -> IndexManifest | None:
    try:
        return load_manifest(lock.layout.repository_root)
    except (IndexManifestNotFoundError, UnsupportedIndexSchemaError):
        return None


def _analyzer_for(project_file: ProjectFile) -> AnalyzerIdentity:
    if project_file.language == "Python":
        return PYTHON_ANALYZER
    if project_file.language in SUPPORTED_POLYGLOT_LANGUAGES:
        return POLYGLOT_ANALYZER
    return FALLBACK_ANALYZER


def _record_location(path: str) -> str:
    path_key = hashlib.sha256(path.encode("utf-8")).hexdigest()
    return f"files/{path_key}.facts.json"


def _build_options_digest(max_source_bytes: int) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "codemap_schema_version": CODEMAP_SCHEMA_VERSION,
                "fallback_analyzer": FALLBACK_ANALYZER.model_dump(mode="json"),
                "max_source_bytes": max_source_bytes,
                "polyglot_analyzer": POLYGLOT_ANALYZER.model_dump(mode="json"),
                "python_analyzer": PYTHON_ANALYZER.model_dump(mode="json"),
                "resolver_version": RESOLVER_VERSION,
            }
        )
    ).hexdigest()


def _index_config_digest() -> str:
    return hashlib.sha256(canonical_json_bytes({"structural_only": True})).hexdigest()


__all__ = [
    "StructuralIndexBuildResult",
    "build_structural_index",
    "load_file_code_map",
    "load_orientation_map",
    "load_relationship_graph",
]
