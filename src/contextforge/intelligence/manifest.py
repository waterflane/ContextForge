"""Pure canonical manifest construction and incremental invalidation rules."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from contextforge.intelligence.models import (
    AnalyzerIdentity,
    IndexBuildState,
    IndexedFileState,
    IndexManifest,
    IndexStatus,
    SchemaVersionMetadata,
    analyzer_identity_key,
    calculate_index_statistics,
    validate_portable_relative_path,
)
from contextforge.repositories import ProjectFile, ProjectSnapshot


class IndexComparisonError(ValueError):
    """Raised when current snapshot inputs are ambiguous or noncanonical."""


def canonical_json_bytes(value: object) -> bytes:
    """Serialize canonical UTF-8 JSON with sorted keys and one final LF."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def calculate_source_snapshot_digest(
    current: ProjectSnapshot | Iterable[ProjectFile],
) -> str:
    """Digest portable source identities without roots or modification times."""

    files = _canonical_current_files(current)
    payload = [
        {
            "language": item.language,
            "path": item.path,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
        }
        for item in files
    ]
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def calculate_generation_id(manifest: IndexManifest) -> str:
    """Digest equality-sensitive manifest content, excluding its own ID."""

    payload = manifest.model_dump(mode="json", exclude={"generation_id"})
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def build_index_manifest(
    *,
    build: IndexBuildState,
    files: Iterable[IndexedFileState],
    structural_analyzers: Iterable[AnalyzerIdentity] = (),
    semantic_analyzers: Iterable[AnalyzerIdentity] = (),
    schema_versions: SchemaVersionMetadata | None = None,
) -> IndexManifest:
    """Create a closed, sorted, content-addressed complete manifest."""

    canonical_files = tuple(sorted(files, key=lambda item: item.path))
    structural = _canonical_analyzers(structural_analyzers)
    semantic = _canonical_analyzers(semantic_analyzers)
    versions = schema_versions or SchemaVersionMetadata()
    statistics = calculate_index_statistics(canonical_files)
    draft = IndexManifest(
        schema_versions=versions,
        generation_id="0" * 64,
        build=build,
        files=canonical_files,
        statistics=statistics,
        structural_analyzers=structural,
        semantic_analyzers=semantic,
    )
    return IndexManifest(
        schema_versions=versions,
        generation_id=calculate_generation_id(draft),
        build=build,
        files=canonical_files,
        statistics=statistics,
        structural_analyzers=structural,
        semantic_analyzers=semantic,
    )


def identify_added_files(
    manifest: IndexManifest | None,
    current: ProjectSnapshot | Iterable[ProjectFile],
) -> tuple[ProjectFile, ...]:
    """Return current files whose portable paths are absent from the manifest."""

    current_files = _canonical_current_files(current)
    indexed_paths = (
        set() if manifest is None else {item.path for item in manifest.files}
    )
    return tuple(item for item in current_files if item.path not in indexed_paths)


def identify_changed_files(
    manifest: IndexManifest | None,
    current: ProjectSnapshot | Iterable[ProjectFile],
) -> tuple[ProjectFile, ...]:
    """Return same-path files whose content identity or language changed."""

    if manifest is None:
        return ()
    indexed = {item.path: item for item in manifest.files}
    return tuple(
        item
        for item in _canonical_current_files(current)
        if item.path in indexed and not _source_matches(indexed[item.path], item)
    )


def identify_unchanged_files(
    manifest: IndexManifest | None,
    current: ProjectSnapshot | Iterable[ProjectFile],
) -> tuple[ProjectFile, ...]:
    """Return same-path files with identical hash, size, and language."""

    if manifest is None:
        return ()
    indexed = {item.path: item for item in manifest.files}
    return tuple(
        item
        for item in _canonical_current_files(current)
        if item.path in indexed and _source_matches(indexed[item.path], item)
    )


def identify_deleted_files(
    manifest: IndexManifest | None,
    current: ProjectSnapshot | Iterable[ProjectFile],
) -> tuple[IndexedFileState, ...]:
    """Return indexed file states whose portable paths no longer exist."""

    if manifest is None:
        return ()
    current_paths = {item.path for item in _canonical_current_files(current)}
    return tuple(item for item in manifest.files if item.path not in current_paths)


def identify_stale_analysis(
    manifest: IndexManifest | None,
    current: ProjectSnapshot | Iterable[ProjectFile],
    *,
    expected_analyzer: AnalyzerIdentity,
    build_options_digest: str,
    schema_versions: SchemaVersionMetadata | None = None,
) -> tuple[IndexedFileState, ...]:
    """Return records invalidated by any source, analyzer, schema, or option input."""

    if manifest is None:
        return ()
    if not _is_sha256(build_options_digest):
        raise IndexComparisonError("build_options_digest must be lowercase SHA-256")
    expected_schema = schema_versions or SchemaVersionMetadata()
    current_by_path = {item.path: item for item in _canonical_current_files(current)}
    invalidate_all = (
        manifest.schema_versions != expected_schema
        or manifest.build.build_options_digest != build_options_digest
    )
    return tuple(
        indexed
        for indexed in manifest.files
        if indexed.path in current_by_path
        and (
            invalidate_all
            or not _source_matches(indexed, current_by_path[indexed.path])
            or indexed.analyzer != expected_analyzer
        )
    )


def compare_index_status(
    manifest: IndexManifest | None,
    current: ProjectSnapshot | Iterable[ProjectFile],
    *,
    expected_analyzer: AnalyzerIdentity,
    build_options_digest: str,
    initialized: bool,
    schema_versions: SchemaVersionMetadata | None = None,
) -> IndexStatus:
    """Build the deterministic public status view for one snapshot."""

    added = identify_added_files(manifest, current)
    changed = identify_changed_files(manifest, current)
    unchanged = identify_unchanged_files(manifest, current)
    deleted = identify_deleted_files(manifest, current)
    stale = identify_stale_analysis(
        manifest,
        current,
        expected_analyzer=expected_analyzer,
        build_options_digest=build_options_digest,
        schema_versions=schema_versions,
    )
    return IndexStatus(
        initialized=initialized,
        active_generation_id=(manifest.generation_id if manifest is not None else None),
        added_files=tuple(item.path for item in added),
        changed_files=tuple(item.path for item in changed),
        unchanged_files=tuple(item.path for item in unchanged),
        deleted_files=tuple(item.path for item in deleted),
        stale_analysis=tuple(item.path for item in stale),
    )


def _canonical_analyzers(
    analyzers: Iterable[AnalyzerIdentity],
) -> tuple[AnalyzerIdentity, ...]:
    values = tuple(sorted(analyzers, key=analyzer_identity_key))
    keys = tuple(analyzer_identity_key(value) for value in values)
    if len(keys) != len(set(keys)):
        raise IndexComparisonError("analyzer identities must be unique")
    return values


def _canonical_current_files(
    current: ProjectSnapshot | Iterable[ProjectFile],
) -> tuple[ProjectFile, ...]:
    raw_files = (
        current.files if isinstance(current, ProjectSnapshot) else tuple(current)
    )
    if any(not isinstance(item, ProjectFile) for item in raw_files):
        raise IndexComparisonError("current files must be ProjectFile instances")
    files = tuple(sorted(raw_files, key=lambda item: item.path))
    paths: list[str] = []
    for item in files:
        try:
            path = validate_portable_relative_path(item.path)
        except ValueError as exc:
            raise IndexComparisonError("current file path is not portable") from exc
        paths.append(path)
    if len(paths) != len(set(paths)):
        raise IndexComparisonError("current file paths must be unique")
    return files


def _source_matches(indexed: IndexedFileState, current: ProjectFile) -> bool:
    return (
        indexed.source_sha256 == current.sha256
        and indexed.source_size_bytes == current.size_bytes
        and indexed.language == current.language
    )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )
