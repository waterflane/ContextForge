"""Deterministic relationship graph and repository orientation artifacts."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from contextforge.intelligence.codemap import FileCodeMap, SourceRange, SymbolKind
from contextforge.intelligence.manifest import canonical_json_bytes
from contextforge.intelligence.models import (
    IndexModel,
    Sha256,
    validate_portable_relative_path,
)

GRAPH_SCHEMA_VERSION: Literal[3] = 3
ORIENTATION_MAP_SCHEMA_VERSION: Literal[3] = 3
PAGERANK_DAMPING = 0.85
PAGERANK_MAX_ITERATIONS = 100
PAGERANK_TOLERANCE = 1e-9

NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
EdgeProvenance = Literal["verified", "best-effort-structural", "model-inferred"]
RelationshipKind = Literal[
    "import",
    "reference",
    "call",
    "source-test",
    "entrypoint-handler",
    "config-consumer",
    "contains",
    "export",
]


class RelationshipGraphNode(IndexModel):
    """One addressable file or symbol in the repository graph."""

    node_id: str
    kind: Literal["file", "symbol"]
    path: str
    symbol_id: str | None = None
    name: str | None = None
    qualified_name: str | None = None
    symbol_kind: SymbolKind | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @model_validator(mode="after")
    def validate_identity(self) -> RelationshipGraphNode:
        if self.kind == "file":
            if self.node_id != file_node_id(self.path) or any(
                value is not None
                for value in (
                    self.symbol_id,
                    self.name,
                    self.qualified_name,
                    self.symbol_kind,
                )
            ):
                raise ValueError("file graph nodes must use their canonical identity")
        elif self.symbol_id is None or self.node_id != symbol_node_id(self.symbol_id):
            raise ValueError("symbol graph nodes require their canonical symbol ID")
        return self


class RelationshipGraphEdge(IndexModel):
    """One provenance-bearing relationship between graph nodes."""

    edge_id: Sha256
    kind: RelationshipKind
    source_node_id: str
    target_node_id: str
    source_file_path: str
    source_range: SourceRange | None = None
    provenance: EdgeProvenance
    detection_method: str

    @field_validator("source_file_path")
    @classmethod
    def validate_source_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)


class InferredGraphLink(IndexModel):
    """Grounded semantic link ready for projection into an enriched graph."""

    source_file_path: str
    source_symbol_id: str | None = None
    source_range: SourceRange
    target_file_path: str
    target_symbol_id: str | None = None

    @field_validator("source_file_path", "target_file_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)


class FileGraphMetrics(IndexModel):
    """Deterministic file projection used by retrieval and map rendering."""

    path: str
    pagerank: NonNegativeFloat
    normalized_centrality: NonNegativeFloat
    fan_in: NonNegativeInt
    fan_out: NonNegativeInt
    reverse_dependencies: tuple[str, ...] = ()
    test_connectivity: NonNegativeInt = 0

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @field_validator("reverse_dependencies")
    @classmethod
    def validate_dependencies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        validated = tuple(validate_portable_relative_path(path) for path in value)
        if validated != tuple(sorted(set(validated))):
            raise ValueError("reverse dependencies must be unique and canonical")
        return validated


class RelationshipGraph(IndexModel):
    """Complete deterministic repository relationship graph."""

    schema_version: Literal[3] = GRAPH_SCHEMA_VERSION
    record_kind: Literal["relationship_graph"] = "relationship_graph"
    source_snapshot_digest: Sha256
    nodes: tuple[RelationshipGraphNode, ...]
    edges: tuple[RelationshipGraphEdge, ...]
    file_metrics: tuple[FileGraphMetrics, ...]

    @model_validator(mode="after")
    def validate_canonical_content(self) -> RelationshipGraph:
        node_ids = tuple(item.node_id for item in self.nodes)
        edge_ids = tuple(item.edge_id for item in self.edges)
        metric_paths = tuple(item.path for item in self.file_metrics)
        if node_ids != tuple(sorted(set(node_ids))):
            raise ValueError("graph nodes must be unique and canonical")
        if edge_ids != tuple(sorted(set(edge_ids))):
            raise ValueError("graph edges must be unique and canonical")
        if metric_paths != tuple(sorted(set(metric_paths))):
            raise ValueError("graph metrics must be unique and canonical")
        known = set(node_ids)
        if any(
            edge.source_node_id not in known or edge.target_node_id not in known
            for edge in self.edges
        ):
            raise ValueError("graph edges must reference published nodes")
        file_paths = {node.path for node in self.nodes if node.kind == "file"}
        if set(metric_paths) != file_paths:
            raise ValueError("graph metrics must cover every file node")
        return self


class OrientationFile(IndexModel):
    """Compact all-file orientation entry."""

    path: str
    module: str
    language: str | None
    line_count: NonNegativeInt
    symbol_count: NonNegativeInt
    import_count: NonNegativeInt
    centrality: NonNegativeFloat
    is_entrypoint: bool = False
    is_test: bool = False
    parse_status: str

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)


class OrientationModule(IndexModel):
    """One deterministic directory/module rollup."""

    module: str
    files: tuple[str, ...]
    centrality: NonNegativeFloat

    @field_validator("files")
    @classmethod
    def validate_files(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        validated = tuple(validate_portable_relative_path(path) for path in value)
        if validated != tuple(sorted(set(validated))):
            raise ValueError("module files must be unique and canonical")
        return validated


class OrientationMap(IndexModel):
    """Full structural repository orientation with every file represented."""

    schema_version: Literal[3] = ORIENTATION_MAP_SCHEMA_VERSION
    record_kind: Literal["repository_orientation"] = "repository_orientation"
    source_snapshot_digest: Sha256
    files: tuple[OrientationFile, ...]
    modules: tuple[OrientationModule, ...]

    @model_validator(mode="after")
    def validate_canonical_content(self) -> OrientationMap:
        paths = tuple(item.path for item in self.files)
        modules = tuple(item.module for item in self.modules)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("orientation files must be unique and canonical")
        if modules != tuple(sorted(set(modules))):
            raise ValueError("orientation modules must be unique and canonical")
        if {path for item in self.modules for path in item.files} != set(paths):
            raise ValueError("orientation modules must cover every file")
        return self


def file_node_id(path: str) -> str:
    return f"file:{path}"


def symbol_node_id(symbol_id: str) -> str:
    return f"symbol:{symbol_id}"


def build_relationship_graph(
    code_maps: tuple[FileCodeMap, ...], source_snapshot_digest: str
) -> RelationshipGraph:
    """Project resolved CodeMaps into a canonical graph and file metrics."""

    ordered_maps = tuple(sorted(code_maps, key=lambda item: item.path))
    nodes: dict[str, RelationshipGraphNode] = {}
    for code_map in ordered_maps:
        nodes[file_node_id(code_map.path)] = RelationshipGraphNode(
            node_id=file_node_id(code_map.path), kind="file", path=code_map.path
        )
        for symbol in code_map.symbols:
            nodes[symbol_node_id(symbol.symbol_id)] = RelationshipGraphNode(
                node_id=symbol_node_id(symbol.symbol_id),
                kind="symbol",
                path=code_map.path,
                symbol_id=symbol.symbol_id,
                name=symbol.name,
                qualified_name=symbol.qualified_name,
                symbol_kind=symbol.kind,
            )

    edges: dict[str, RelationshipGraphEdge] = {}
    for code_map in ordered_maps:
        for relationship in code_map.relationships:
            target = relationship.target
            if target.resolution != "internal" or target.file_path is None:
                continue
            source_node = (
                symbol_node_id(relationship.source_symbol_id)
                if relationship.source_symbol_id is not None
                else file_node_id(code_map.path)
            )
            target_node = (
                symbol_node_id(target.symbol_id)
                if target.symbol_id is not None
                else file_node_id(target.file_path)
            )
            if source_node not in nodes or target_node not in nodes:
                continue
            kind = _relationship_kind(relationship.kind)
            edge = _edge(
                kind,
                source_node,
                target_node,
                code_map.path,
                relationship.source_range,
                "verified",
                relationship.detection_method,
            )
            edges[edge.edge_id] = edge

    _add_entrypoint_edges(ordered_maps, nodes, edges)
    _add_config_consumer_edges(ordered_maps, nodes, edges)
    canonical_nodes = tuple(sorted(nodes.values(), key=lambda item: item.node_id))
    canonical_edges = tuple(sorted(edges.values(), key=lambda item: item.edge_id))
    metrics = _file_metrics(ordered_maps, canonical_nodes, canonical_edges)
    return RelationshipGraph(
        source_snapshot_digest=source_snapshot_digest,
        nodes=canonical_nodes,
        edges=canonical_edges,
        file_metrics=metrics,
    )


def build_orientation_map(
    code_maps: tuple[FileCodeMap, ...], graph: RelationshipGraph
) -> OrientationMap:
    """Aggregate a full all-file orientation map without model calls."""

    metrics = {item.path: item for item in graph.file_metrics}
    files = tuple(
        OrientationFile(
            path=code_map.path,
            module=_module_for_path(code_map.path),
            language=code_map.language,
            line_count=code_map.line_count,
            symbol_count=len(code_map.symbols),
            import_count=len(code_map.imports),
            centrality=metrics[code_map.path].normalized_centrality,
            is_entrypoint=_is_entrypoint(code_map.path),
            is_test=_is_test_path(code_map.path),
            parse_status=code_map.parse_status,
        )
        for code_map in sorted(code_maps, key=lambda item: item.path)
    )
    grouped: dict[str, list[OrientationFile]] = defaultdict(list)
    for item in files:
        grouped[item.module].append(item)
    modules = tuple(
        OrientationModule(
            module=module,
            files=tuple(item.path for item in items),
            centrality=sum(item.centrality for item in items),
        )
        for module, items in sorted(grouped.items())
    )
    return OrientationMap(
        source_snapshot_digest=graph.source_snapshot_digest,
        files=files,
        modules=modules,
    )


def add_model_inferred_edges(
    graph: RelationshipGraph, links: tuple[InferredGraphLink, ...]
) -> RelationshipGraph:
    """Replace inferred projections without changing structural centrality metrics."""

    known = {item.node_id for item in graph.nodes}
    edges = {
        item.edge_id: item
        for item in graph.edges
        if item.provenance != "model-inferred"
    }
    for link in links:
        source_node_id = (
            symbol_node_id(link.source_symbol_id)
            if link.source_symbol_id is not None
            else file_node_id(link.source_file_path)
        )
        target_node_id = (
            symbol_node_id(link.target_symbol_id)
            if link.target_symbol_id is not None
            else file_node_id(link.target_file_path)
        )
        if source_node_id not in known or target_node_id not in known:
            continue
        edge = _edge(
            "reference",
            source_node_id,
            target_node_id,
            link.source_file_path,
            link.source_range,
            "model-inferred",
            "semantic_card_closed_candidate",
        )
        edges[edge.edge_id] = edge
    return graph.model_copy(
        update={"edges": tuple(sorted(edges.values(), key=lambda item: item.edge_id))}
    )


def _relationship_kind(kind: str) -> RelationshipKind:
    if kind in {"tests", "tested_by", "test_reference"}:
        return "source-test"
    direct: dict[str, RelationshipKind] = {
        "import": "import",
        "call": "call",
        "reference": "reference",
        "contains": "contains",
        "export": "export",
    }
    try:
        return direct[kind]
    except KeyError as exc:
        raise ValueError(f"unsupported structural relationship kind: {kind}") from exc


def _edge(
    kind: RelationshipKind,
    source_node_id: str,
    target_node_id: str,
    source_file_path: str,
    source_range: SourceRange | None,
    provenance: EdgeProvenance,
    detection_method: str,
) -> RelationshipGraphEdge:
    payload = {
        "kind": kind,
        "source": source_node_id,
        "target": target_node_id,
        "source_file": source_file_path,
        "source_range": (
            None if source_range is None else source_range.model_dump(mode="json")
        ),
        "provenance": provenance,
        "detection_method": detection_method,
    }
    return RelationshipGraphEdge(
        edge_id=hashlib.sha256(canonical_json_bytes(payload)).hexdigest(),
        kind=kind,
        source_node_id=source_node_id,
        target_node_id=target_node_id,
        source_file_path=source_file_path,
        source_range=source_range,
        provenance=provenance,
        detection_method=detection_method,
    )


def _add_entrypoint_edges(
    code_maps: tuple[FileCodeMap, ...],
    nodes: dict[str, RelationshipGraphNode],
    edges: dict[str, RelationshipGraphEdge],
) -> None:
    entrypoints = {item.path for item in code_maps if _is_entrypoint(item.path)}
    for existing in tuple(edges.values()):
        if existing.source_file_path not in entrypoints or existing.kind not in {
            "import",
            "call",
        }:
            continue
        edge = _edge(
            "entrypoint-handler",
            file_node_id(existing.source_file_path),
            existing.target_node_id,
            existing.source_file_path,
            existing.source_range,
            "best-effort-structural",
            "entrypoint_import_or_call",
        )
        if edge.target_node_id in nodes:
            edges[edge.edge_id] = edge


def _add_config_consumer_edges(
    code_maps: tuple[FileCodeMap, ...],
    nodes: dict[str, RelationshipGraphNode],
    edges: dict[str, RelationshipGraphEdge],
) -> None:
    configs = [item for item in code_maps if _is_config_path(item.path)]
    consumers = [
        item
        for item in code_maps
        if any(symbol.configuration_keys for symbol in item.symbols)
    ]
    for config in configs:
        repository_wide = _is_repository_configuration(config.path)
        for consumer in consumers:
            if consumer.path == config.path:
                continue
            if not repository_wide and not _is_module_configuration_consumer(
                config.path, consumer.path
            ):
                continue
            edge = _edge(
                "config-consumer",
                file_node_id(config.path),
                file_node_id(consumer.path),
                config.path,
                None,
                "best-effort-structural",
                (
                    "repository_configuration_key_consumer"
                    if repository_wide
                    else "module_local_configuration_key_consumer"
                ),
            )
            if edge.source_node_id in nodes and edge.target_node_id in nodes:
                edges[edge.edge_id] = edge


def _file_metrics(
    code_maps: tuple[FileCodeMap, ...],
    nodes: tuple[RelationshipGraphNode, ...],
    edges: tuple[RelationshipGraphEdge, ...],
) -> tuple[FileGraphMetrics, ...]:
    node_path = {item.node_id: item.path for item in nodes}
    paths = tuple(item.path for item in code_maps)
    outgoing: dict[str, set[str]] = {path: set() for path in paths}
    incoming: dict[str, set[str]] = {path: set() for path in paths}
    test_neighbors: dict[str, set[str]] = {path: set() for path in paths}
    for edge in edges:
        source = node_path[edge.source_node_id]
        target = node_path[edge.target_node_id]
        if source == target:
            continue
        if edge.kind == "source-test":
            test_neighbors[source].add(target)
            test_neighbors[target].add(source)
            continue
        if edge.provenance == "model-inferred":
            continue
        outgoing[source].add(target)
        incoming[target].add(source)
    ranks = _pagerank(paths, outgoing)
    maximum = max(ranks.values(), default=0.0)
    return tuple(
        FileGraphMetrics(
            path=path,
            pagerank=ranks[path],
            normalized_centrality=(ranks[path] / maximum if maximum else 0.0),
            fan_in=len(incoming[path]),
            fan_out=len(outgoing[path]),
            reverse_dependencies=tuple(sorted(incoming[path])),
            test_connectivity=len(test_neighbors[path]),
        )
        for path in sorted(paths)
    )


def _pagerank(
    paths: tuple[str, ...], outgoing: dict[str, set[str]]
) -> dict[str, float]:
    ordered = tuple(sorted(paths))
    count = len(ordered)
    if count == 0:
        return {}
    rank = {path: 1.0 / count for path in ordered}
    teleport = (1.0 - PAGERANK_DAMPING) / count
    for _ in range(PAGERANK_MAX_ITERATIONS):
        dangling = sum(rank[path] for path in ordered if not outgoing[path]) / count
        next_rank = {path: teleport + PAGERANK_DAMPING * dangling for path in ordered}
        for source in ordered:
            targets = outgoing[source]
            if not targets:
                continue
            contribution = PAGERANK_DAMPING * rank[source] / len(targets)
            for target in sorted(targets):
                next_rank[target] += contribution
        delta = sum(abs(next_rank[path] - rank[path]) for path in ordered)
        rank = next_rank
        if delta <= PAGERANK_TOLERANCE:
            break
    return rank


def _module_for_path(path: str) -> str:
    parent = PurePosixPath(path).parent.as_posix()
    return "root" if parent == "." else parent


def _is_entrypoint(path: str) -> bool:
    pure = PurePosixPath(path)
    stem = pure.stem.casefold()
    return stem in {"__main__", "main", "app", "server", "cli", "manage"} or (
        len(pure.parts) <= 2 and stem in {"index", "bootstrap", "startup"}
    )


def _is_test_path(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name.casefold()
    return any(
        part.casefold() in {"test", "tests", "spec", "specs"} for part in pure.parts
    ) or name.startswith(("test_", "spec_"))


def _is_config_path(path: str) -> bool:
    pure = PurePosixPath(path)
    name = pure.name.casefold()
    parts = tuple(part.casefold() for part in pure.parts)
    conventional_directory = any(
        part in {"config", "configs", "configuration", "settings"}
        for part in parts[:-1]
    )
    return (
        pure.suffix.casefold()
        in {
            ".toml",
            ".yaml",
            ".yml",
            ".ini",
            ".cfg",
        }
        or (conventional_directory and pure.suffix.casefold() in {".json", ".py"})
        or name
        in {
            ".env",
            "dockerfile",
            "pyproject.toml",
            "package.json",
        }
        or (
            pure.stem.casefold() in {"config", "configuration", "settings"}
            and pure.suffix.casefold() in {".json", ".py"}
        )
    )


def _is_repository_configuration(path: str) -> bool:
    pure = PurePosixPath(path)
    parts = tuple(part.casefold() for part in pure.parts)
    if len(parts) > 1 and parts[0] in {
        "config",
        "configs",
        "configuration",
        "settings",
    }:
        return True
    if len(parts) != 1:
        return False
    name = parts[0]
    stem = pure.stem.casefold()
    return name in {
        ".env",
        "dockerfile",
        "package.json",
        "pyproject.toml",
    } or stem in {"config", "configuration", "settings"}


def _is_module_configuration_consumer(config_path: str, consumer_path: str) -> bool:
    config_parent = PurePosixPath(config_path).parent
    if config_parent.name.casefold() in {
        "config",
        "configs",
        "configuration",
        "settings",
    }:
        config_parent = config_parent.parent
    scope = () if config_parent.as_posix() == "." else config_parent.parts
    consumer_parent = PurePosixPath(consumer_path).parent.parts
    return bool(scope) and consumer_parent[: len(scope)] == scope


__all__ = [
    "GRAPH_SCHEMA_VERSION",
    "ORIENTATION_MAP_SCHEMA_VERSION",
    "PAGERANK_DAMPING",
    "PAGERANK_MAX_ITERATIONS",
    "PAGERANK_TOLERANCE",
    "EdgeProvenance",
    "FileGraphMetrics",
    "InferredGraphLink",
    "OrientationFile",
    "OrientationMap",
    "OrientationModule",
    "RelationshipGraph",
    "RelationshipGraphEdge",
    "RelationshipGraphNode",
    "RelationshipKind",
    "build_orientation_map",
    "build_relationship_graph",
    "add_model_inferred_edges",
    "file_node_id",
    "symbol_node_id",
]
