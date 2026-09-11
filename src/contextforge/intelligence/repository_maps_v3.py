"""Deterministic repository maps aggregated from CodeMaps and grounded cards."""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

from pydantic import Field, field_validator, model_validator

from contextforge.intelligence.codemap import FileCodeMap, SourceRange
from contextforge.intelligence.graph import (
    EdgeProvenance,
    RelationshipGraph,
    RelationshipKind,
    build_relationship_graph,
)
from contextforge.intelligence.manifest import canonical_json_bytes
from contextforge.intelligence.models import (
    IndexManifest,
    IndexModel,
    Sha256,
    validate_portable_relative_path,
)
from contextforge.intelligence.store import load_generation_record, load_manifest

if TYPE_CHECKING:
    from contextforge.intelligence.cards import GroundedClaim, SemanticCard

REPOSITORY_MAP_SCHEMA_VERSION: Literal[3] = 3
RepositoryMapKind = Literal["architecture", "conventions", "features"]
REPOSITORY_MAP_KINDS: tuple[RepositoryMapKind, ...] = (
    "architecture",
    "conventions",
    "features",
)
RepositoryClaimKind = Literal["concept", "responsibility"]
RepositoryClaimProvenance = Literal[
    "verified",
    "best-effort-structural",
    "model-inferred",
    "grounded-semantic-card",
]


class RepositoryMapClaim(IndexModel):
    """One typed interpretation whose evidence remains addressable in a card."""

    claim_id: Sha256
    kind: RepositoryClaimKind
    text: str = Field(min_length=1, max_length=2_000)
    paths: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    provenance: RepositoryClaimProvenance

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        validated = tuple(validate_portable_relative_path(path) for path in value)
        if validated != tuple(sorted(set(validated))):
            raise ValueError("repository claim paths must be unique and canonical")
        return validated

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("repository claim evidence must be unique and canonical")
        return value


class RepositoryMapRelationship(IndexModel):
    """One graph projection retaining its structural or inferred provenance."""

    relationship_id: Sha256
    kind: RelationshipKind
    source_path: str
    target_path: str
    source_symbol_id: str | None = None
    target_symbol_id: str | None = None
    source_range: SourceRange | None = None
    provenance: EdgeProvenance
    detection_method: str = Field(min_length=1, max_length=200)

    @field_validator("source_path", "target_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)


class RepositoryMapEntry(IndexModel):
    """One deterministic map group with canonical member paths."""

    name: str
    paths: tuple[str, ...]
    attributes: dict[str, str | int | float | bool]
    claims: tuple[RepositoryMapClaim, ...] = ()
    relationships: tuple[RepositoryMapRelationship, ...] = ()

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        validated = tuple(validate_portable_relative_path(path) for path in value)
        if validated != tuple(sorted(set(validated))):
            raise ValueError("repository map paths must be unique and canonical")
        return validated

    @model_validator(mode="after")
    def validate_typed_content(self) -> RepositoryMapEntry:
        claim_ids = tuple(item.claim_id for item in self.claims)
        relationship_ids = tuple(item.relationship_id for item in self.relationships)
        if claim_ids != tuple(sorted(set(claim_ids))):
            raise ValueError("repository map claims must be unique and canonical")
        if relationship_ids != tuple(sorted(set(relationship_ids))):
            raise ValueError(
                "repository map relationships must be unique and canonical"
            )
        return self


class RepositoryMap(IndexModel):
    """One model-free repository aggregation."""

    schema_version: Literal[3] = REPOSITORY_MAP_SCHEMA_VERSION
    record_kind: Literal["repository_map"] = "repository_map"
    map_kind: RepositoryMapKind
    source_snapshot_digest: Sha256
    entries: tuple[RepositoryMapEntry, ...]

    @model_validator(mode="after")
    def validate_entries(self) -> RepositoryMap:
        names = tuple(item.name for item in self.entries)
        if names != tuple(sorted(set(names))):
            raise ValueError("repository map entries must be unique and canonical")
        return self


def build_repository_maps_v3(
    code_maps: tuple[FileCodeMap, ...],
    cards: tuple[SemanticCard, ...],
    source_snapshot_digest: str,
    *,
    relationship_graph: RelationshipGraph | None = None,
) -> tuple[RepositoryMap, RepositoryMap, RepositoryMap]:
    """Build architecture, conventions, and features without provider calls."""

    graph = relationship_graph or build_relationship_graph(
        code_maps, source_snapshot_digest
    )
    if graph.source_snapshot_digest != source_snapshot_digest:
        raise ValueError("repository maps require the current relationship graph")
    relationships = _map_relationships(graph)
    metrics = {item.path: item for item in graph.file_metrics}
    cards_by_path = {item.path: item for item in cards}
    architecture_groups: dict[str, list[FileCodeMap]] = defaultdict(list)
    for code_map in code_maps:
        parent = PurePosixPath(code_map.path).parent.as_posix()
        architecture_groups["root" if parent == "." else parent].append(code_map)
    architecture = RepositoryMap(
        map_kind="architecture",
        source_snapshot_digest=source_snapshot_digest,
        entries=tuple(
            RepositoryMapEntry(
                name=name,
                paths=tuple(sorted(item.path for item in items)),
                attributes={
                    "files": len(items),
                    "symbols": sum(len(item.symbols) for item in items),
                    "imports": sum(len(item.imports) for item in items),
                    "centrality": round(
                        sum(metrics[item.path].normalized_centrality for item in items),
                        12,
                    ),
                    "entrypoints": sum(_is_entrypoint(item.path) for item in items),
                    "test_connectivity": sum(
                        metrics[item.path].test_connectivity for item in items
                    ),
                },
                claims=_claims_for_paths(
                    tuple(item.path for item in items), cards_by_path
                ),
                relationships=_relationships_for_paths(
                    tuple(item.path for item in items), relationships
                ),
            )
            for name, items in sorted(architecture_groups.items())
        ),
    )

    suffixes = Counter(
        PurePosixPath(item.path).suffix.casefold() or "(none)" for item in code_maps
    )
    tests = tuple(sorted(item.path for item in code_maps if _is_test(item.path)))
    configs = tuple(sorted(card.path for card in cards if card.profile == "config"))
    convention_entries = [
        RepositoryMapEntry(
            name=f"extension:{suffix}",
            paths=tuple(
                sorted(
                    item.path
                    for item in code_maps
                    if (PurePosixPath(item.path).suffix.casefold() or "(none)")
                    == suffix
                )
            ),
            attributes={"count": count},
            relationships=_relationships_for_paths(
                tuple(
                    item.path
                    for item in code_maps
                    if (PurePosixPath(item.path).suffix.casefold() or "(none)")
                    == suffix
                ),
                relationships,
                kinds={"import", "export", "reference", "call"},
            ),
        )
        for suffix, count in sorted(suffixes.items())
    ]
    if tests:
        convention_entries.append(
            RepositoryMapEntry(
                name="test-layout",
                paths=tests,
                attributes={
                    "count": len(tests),
                    "connected_files": sum(
                        metrics[path].test_connectivity for path in tests
                    ),
                },
                relationships=_relationships_for_paths(
                    tests, relationships, kinds={"source-test"}
                ),
            )
        )
    if configs:
        convention_entries.append(
            RepositoryMapEntry(
                name="configuration-layout",
                paths=configs,
                attributes={"count": len(configs)},
                relationships=_relationships_for_paths(
                    configs, relationships, kinds={"config-consumer"}
                ),
            )
        )
    conventions = RepositoryMap(
        map_kind="conventions",
        source_snapshot_digest=source_snapshot_digest,
        entries=tuple(sorted(convention_entries, key=lambda item: item.name)),
    )

    feature_groups: dict[str, list[tuple[SemanticCard, GroundedClaim]]] = defaultdict(
        list
    )
    for card in cards:
        for concept in card.concepts:
            feature_groups[_feature_name(concept.text)].append((card, concept))
    features = RepositoryMap(
        map_kind="features",
        source_snapshot_digest=source_snapshot_digest,
        entries=tuple(
            RepositoryMapEntry(
                name=name,
                paths=tuple(sorted({card.path for card, _ in items})),
                attributes={
                    "cards": len({card.path for card, _ in items}),
                    "grounded_concepts": len(items),
                },
                claims=tuple(
                    sorted(
                        {
                            claim.claim_id: claim
                            for card, concept in items
                            for claim in (
                                _map_claim(card, concept, "concept"),
                                *(
                                    _map_claim(card, responsibility, "responsibility")
                                    for responsibility in card.responsibilities
                                ),
                            )
                        }.values(),
                        key=lambda item: item.claim_id,
                    )
                ),
                relationships=_relationships_for_paths(
                    tuple(sorted({card.path for card, _ in items})), relationships
                ),
            )
            for name, items in sorted(feature_groups.items())
        ),
    )
    return architecture, conventions, features


def load_repository_map_v3(
    repository_root: str | Path,
    kind: RepositoryMapKind,
    *,
    manifest: IndexManifest | None = None,
) -> RepositoryMap:
    """Load one digest-checked deterministic v3 repository map."""

    active = manifest if manifest is not None else load_manifest(repository_root)
    reference = {
        "architecture": active.artifacts.architecture_map,
        "conventions": active.artifacts.conventions_map,
        "features": active.artifacts.features_map,
    }[kind]
    if reference is None:
        raise ValueError(f"{kind} repository map is absent from the generation")
    content = load_generation_record(
        repository_root, reference.location, manifest=active
    )
    if hashlib.sha256(content).hexdigest() != reference.sha256:
        raise ValueError(f"{kind} repository map digest does not match the manifest")
    result = RepositoryMap.model_validate_json(content)
    if (
        result.map_kind != kind
        or result.source_snapshot_digest != active.build.source_snapshot_digest
    ):
        raise ValueError(
            f"{kind} repository map identity does not match the generation"
        )
    return result


def _map_relationships(
    graph: RelationshipGraph,
) -> tuple[RepositoryMapRelationship, ...]:
    nodes = {item.node_id: item for item in graph.nodes}
    return tuple(
        sorted(
            (
                RepositoryMapRelationship(
                    relationship_id=edge.edge_id,
                    kind=edge.kind,
                    source_path=nodes[edge.source_node_id].path,
                    target_path=nodes[edge.target_node_id].path,
                    source_symbol_id=nodes[edge.source_node_id].symbol_id,
                    target_symbol_id=nodes[edge.target_node_id].symbol_id,
                    source_range=edge.source_range,
                    provenance=edge.provenance,
                    detection_method=edge.detection_method,
                )
                for edge in graph.edges
            ),
            key=lambda item: item.relationship_id,
        )
    )


def _relationships_for_paths(
    paths: tuple[str, ...],
    relationships: tuple[RepositoryMapRelationship, ...],
    *,
    kinds: set[RelationshipKind] | None = None,
) -> tuple[RepositoryMapRelationship, ...]:
    selected = set(paths)
    return tuple(
        item
        for item in relationships
        if (item.source_path in selected or item.target_path in selected)
        and (kinds is None or item.kind in kinds)
    )


def _claims_for_paths(
    paths: tuple[str, ...], cards: dict[str, SemanticCard]
) -> tuple[RepositoryMapClaim, ...]:
    values: list[RepositoryMapClaim] = []
    for path in paths:
        card = cards.get(path)
        if card is None:
            continue
        values.extend(_map_claim(card, claim, "concept") for claim in card.concepts)
        values.extend(
            _map_claim(card, claim, "responsibility") for claim in card.responsibilities
        )
    return tuple(sorted(values, key=lambda item: item.claim_id))


def _map_claim(
    card: SemanticCard,
    claim: GroundedClaim,
    kind: RepositoryClaimKind,
) -> RepositoryMapClaim:
    evidence_ids = tuple(f"{card.path}#{item}" for item in claim.evidence_ids)
    claim_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "path": card.path,
                "kind": kind,
                "text": claim.text,
                "evidence_ids": evidence_ids,
                "source_sha256": card.source_sha256,
            }
        )
    ).hexdigest()
    return RepositoryMapClaim(
        claim_id=claim_id,
        kind=kind,
        text=claim.text,
        paths=(card.path,),
        evidence_ids=evidence_ids,
        provenance="grounded-semantic-card",
    )


def _feature_name(text: str) -> str:
    normalized = "-".join(
        item for item in re.findall(r"[^\W_]+", text.casefold()) if item
    )[:80]
    digest = hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()[:12]
    return f"concept:{normalized or 'unnamed'}:{digest}"


def _is_entrypoint(path: str) -> bool:
    return PurePosixPath(path).stem.casefold() in {
        "__main__",
        "app",
        "cli",
        "main",
        "manage",
        "server",
    }


def _is_test(path: str) -> bool:
    pure = PurePosixPath(path)
    return any(
        part.casefold() in {"test", "tests", "spec", "specs"} for part in pure.parts
    ) or pure.name.casefold().startswith(("test_", "spec_"))


__all__ = [
    "REPOSITORY_MAP_KINDS",
    "REPOSITORY_MAP_SCHEMA_VERSION",
    "RepositoryMap",
    "RepositoryMapClaim",
    "RepositoryMapEntry",
    "RepositoryMapKind",
    "RepositoryMapRelationship",
    "build_repository_maps_v3",
    "load_repository_map_v3",
]
