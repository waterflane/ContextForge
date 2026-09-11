import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from contextforge.application import build_repository_index
from contextforge.intelligence import (
    DETERMINISTIC_CARD_ANALYZER,
    FileGraphMetrics,
    GroundedClaim,
    IndexManifestReadError,
    OrientationFile,
    OrientationMap,
    OrientationModule,
    RelationshipGraph,
    RelationshipGraphEdge,
    RelationshipGraphNode,
    SemanticCard,
    SemanticCardOptions,
    SemanticCardProvenance,
    SemanticEvidence,
    SemanticKeySymbol,
    SourceRange,
    SymbolKind,
    acquire_index_lock,
    build_orientation_map,
    build_relationship_graph,
    build_structural_index,
    extract_code_maps,
    initialize_index,
    load_file_code_map,
    load_orientation_map,
    load_relationship_graph,
)
from contextforge.intelligence.repository_maps_v3 import (
    RepositoryMap,
    RepositoryMapEntry,
)
from contextforge.models import FakeModelProvider, ProviderConfiguration
from contextforge.repositories import scan_repository

DIGEST = "0" * 64


def _claim() -> GroundedClaim:
    return GroundedClaim(text="Grounded purpose", evidence_ids=("symbol",))


def _card() -> SemanticCard:
    return SemanticCard(
        path="module.py",
        source_sha256=DIGEST,
        facts_sha256=DIGEST,
        profile="code",
        provenance=SemanticCardProvenance(
            analyzer=DETERMINISTIC_CARD_ANALYZER,
            method="deterministic-policy",
        ),
        synopsis=_claim(),
        concepts=(_claim(),),
        key_symbols=(
            SemanticKeySymbol(
                symbol_id="verified-symbol",
                name="run",
                qualified_name="run",
                kind=SymbolKind.FUNCTION,
                evidence_ids=("symbol",),
            ),
        ),
        evidence=(
            SemanticEvidence(
                evidence_id="symbol",
                path="module.py",
                source_sha256=DIGEST,
                source_range=SourceRange(
                    start_line=1, start_column=0, end_line=2, end_column=0
                ),
                symbol_id="verified-symbol",
            ),
        ),
        quality="deterministic",
    )


def test_semantic_v3_schemas_enforce_grounding_and_canonical_ids() -> None:
    with pytest.raises(ValidationError, match="range or verified fact"):
        SemanticEvidence(
            evidence_id="empty",
            path="module.py",
            source_sha256=DIGEST,
        )
    with pytest.raises(ValidationError, match="unique and canonical"):
        GroundedClaim(text="bad", evidence_ids=("z", "a", "z"))
    with pytest.raises(ValidationError, match="unique and canonical"):
        SemanticKeySymbol(
            symbol_id="s",
            name="s",
            qualified_name="s",
            kind=SymbolKind.FUNCTION,
            evidence_ids=("z", "a"),
        )

    payload = _card().model_dump(mode="json")
    payload["concepts"][0]["evidence_ids"] = ["unknown"]
    with pytest.raises(ValidationError, match="unknown evidence"):
        SemanticCard.model_validate(payload)

    payload = _card().model_dump(mode="json")
    payload["key_symbols"][0]["symbol_id"] = "invented"
    with pytest.raises(ValidationError, match="not bound"):
        SemanticCard.model_validate(payload)

    payload = _card().model_dump(mode="json")
    payload["evidence"].append(dict(payload["evidence"][0]))
    with pytest.raises(ValidationError, match="unique and canonical"):
        SemanticCard.model_validate(payload)

    payload = _card().model_dump(mode="json")
    payload["profile_facts"] = {"z": [], "a": []}
    with pytest.raises(ValidationError, match="keys must be canonical"):
        SemanticCard.model_validate(payload)


def test_semantic_scheduler_limits_are_strict() -> None:
    with pytest.raises(ValueError, match="priority, all, or none"):
        SemanticCardOptions(scope="invalid")  # type: ignore[arg-type]
    for field in (
        "max_model_files",
        "max_requests",
        "max_estimated_input_tokens",
        "max_chunks_per_file",
        "max_output_tokens",
    ):
        with pytest.raises(ValueError, match="positive integer"):
            SemanticCardOptions(**{field: 0})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at most four"):
        SemanticCardOptions(max_chunks_per_file=5)


def test_graph_and_repository_map_schemas_reject_inconsistent_projections() -> None:
    with pytest.raises(ValidationError, match="canonical identity"):
        RelationshipGraphNode(node_id="wrong", kind="file", path="module.py")
    with pytest.raises(ValidationError, match="canonical symbol ID"):
        RelationshipGraphNode(node_id="symbol:none", kind="symbol", path="module.py")
    with pytest.raises(ValidationError, match="unique and canonical"):
        FileGraphMetrics(
            path="module.py",
            pagerank=1.0,
            normalized_centrality=1.0,
            fan_in=0,
            fan_out=0,
            reverse_dependencies=("z.py", "a.py"),
        )

    node = RelationshipGraphNode(
        node_id="file:module.py", kind="file", path="module.py"
    )
    metric = FileGraphMetrics(
        path="module.py",
        pagerank=1.0,
        normalized_centrality=1.0,
        fan_in=0,
        fan_out=0,
    )
    with pytest.raises(ValidationError, match="nodes must be unique"):
        RelationshipGraph(
            source_snapshot_digest=DIGEST,
            nodes=(node, node),
            edges=(),
            file_metrics=(metric,),
        )
    dangling = RelationshipGraphEdge(
        edge_id=DIGEST,
        kind="reference",
        source_node_id=node.node_id,
        target_node_id="file:missing.py",
        source_file_path="module.py",
        provenance="verified",
        detection_method="test",
    )
    with pytest.raises(ValidationError, match="published nodes"):
        RelationshipGraph(
            source_snapshot_digest=DIGEST,
            nodes=(node,),
            edges=(dangling,),
            file_metrics=(metric,),
        )
    with pytest.raises(ValidationError, match="cover every file"):
        RelationshipGraph(
            source_snapshot_digest=DIGEST,
            nodes=(node,),
            edges=(),
            file_metrics=(),
        )

    file = OrientationFile(
        path="module.py",
        module="root",
        language="Python",
        line_count=1,
        symbol_count=0,
        import_count=0,
        centrality=1.0,
        parse_status="parsed",
    )
    module = OrientationModule(module="root", files=("module.py",), centrality=1.0)
    with pytest.raises(ValidationError, match="files must be unique"):
        OrientationMap(
            source_snapshot_digest=DIGEST,
            files=(file, file),
            modules=(module,),
        )
    with pytest.raises(ValidationError, match="modules must be unique"):
        OrientationMap(
            source_snapshot_digest=DIGEST,
            files=(file,),
            modules=(module, module),
        )
    with pytest.raises(ValidationError, match="cover every file"):
        OrientationMap(
            source_snapshot_digest=DIGEST,
            files=(file,),
            modules=(),
        )
    with pytest.raises(ValidationError, match="paths must be unique"):
        RepositoryMapEntry(name="bad", paths=("z.py", "a.py"), attributes={})
    entry = RepositoryMapEntry(name="same", paths=("module.py",), attributes={})
    with pytest.raises(ValidationError, match="entries must be unique"):
        RepositoryMap(
            map_kind="architecture",
            source_snapshot_digest=DIGEST,
            entries=(entry, entry),
        )


def test_graph_builds_config_consumer_edges_and_empty_graph(tmp_path: Path) -> None:
    (tmp_path / "settings.toml").write_text("port = 8000\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "import os\n\ndef start():\n    return os.getenv('PORT')\n",
        encoding="utf-8",
    )
    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=None,
            provider_configuration=None,
        )
    )
    graph = load_relationship_graph(tmp_path, manifest=report.manifest)
    assert any(edge.kind == "config-consumer" for edge in graph.edges)


def test_config_consumers_respect_repository_and_module_scope(tmp_path: Path) -> None:
    (tmp_path / "settings.toml").write_text("port = 8000\n", encoding="utf-8")
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "local.toml").write_text("mode = 'safe'\n", encoding="utf-8")
    (package / "service.py").write_text(
        "import os\nVALUE = os.environ.get('MODE')\n", encoding="utf-8"
    )
    other = tmp_path / "other"
    other.mkdir()
    (other / "service.py").write_text(
        "import os\nVALUE = os.getenv('PORT')\n", encoding="utf-8"
    )

    snapshot = scan_repository(tmp_path)
    code_maps = extract_code_maps(snapshot)
    graph = build_relationship_graph(code_maps, "1" * 64)
    consumers = {
        (edge.source_file_path, graph_node.path)
        for edge in graph.edges
        if edge.kind == "config-consumer"
        for graph_node in graph.nodes
        if graph_node.node_id == edge.target_node_id
    }

    assert ("settings.toml", "pkg/service.py") not in consumers
    assert ("settings.toml", "other/service.py") in consumers
    assert ("pkg/local.toml", "pkg/service.py") in consumers
    assert ("pkg/local.toml", "other/service.py") not in consumers


def test_source_test_edges_only_affect_test_connectivity(tmp_path: Path) -> None:
    (tmp_path / "service.py").write_text(
        "def handle():\n    return 'ok'\n", encoding="utf-8"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_service.py").write_text(
        "from service import handle\n\n"
        "def test_handle():\n"
        "    assert handle() == 'ok'\n",
        encoding="utf-8",
    )

    snapshot = scan_repository(tmp_path)
    code_maps = extract_code_maps(snapshot)
    test_only_maps = tuple(
        code_map.model_copy(
            update={
                "relationships": tuple(
                    relationship
                    for relationship in code_map.relationships
                    if relationship.kind in {"tests", "tested_by", "test_reference"}
                )
            }
        )
        for code_map in code_maps
    )
    graph = build_relationship_graph(test_only_maps, "2" * 64)
    metrics = {item.path: item for item in graph.file_metrics}

    assert metrics["service.py"].fan_in == 0
    assert metrics["service.py"].fan_out == 0
    assert metrics["service.py"].reverse_dependencies == ()
    assert metrics["service.py"].test_connectivity == 1
    assert metrics["tests/test_service.py"].test_connectivity == 1
    assert metrics["service.py"].pagerank == pytest.approx(0.5)
    assert metrics["tests/test_service.py"].pagerank == pytest.approx(0.5)


def test_model_failure_uses_grounded_deterministic_fallback(tmp_path: Path) -> None:
    (tmp_path / "service.py").write_text(
        "def serve(value: str) -> str:\n"
        "    if not value:\n"
        "        raise ValueError('empty')\n"
        "    normalized = value.strip()\n"
        "    if not normalized:\n"
        "        raise ValueError('blank')\n"
        "    return normalized\n",
        encoding="utf-8",
    )
    configuration = ProviderConfiguration(
        provider_id="fake",
        endpoint="http://127.0.0.1:1",
        model_id="semantic-failure",
        retry_limit=0,
        max_json_repair_attempts=0,
    )
    provider = FakeModelProvider(
        configuration,
        scripts=(RuntimeError("first"), RuntimeError("repair")),
    )

    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=configuration,
        )
    )

    assert report.semantic is not None
    assert report.semantic.failed_paths == ("service.py",)  # type: ignore[union-attr]
    assert provider.call_count == 2


def test_v3_artifact_loaders_reject_absent_wrong_and_stale_records(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def b():\n    return 2\n", encoding="utf-8")
    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=None,
            provider_configuration=None,
        )
    )
    manifest = report.manifest
    artifacts = manifest.artifacts
    assert artifacts.relationship_graph is not None
    assert artifacts.orientation_map is not None

    no_graph = manifest.model_copy(
        update={"artifacts": artifacts.model_copy(update={"relationship_graph": None})}
    )
    with pytest.raises(IndexManifestReadError, match="no relationship graph"):
        load_relationship_graph(tmp_path, manifest=no_graph)
    no_orientation = manifest.model_copy(
        update={"artifacts": artifacts.model_copy(update={"orientation_map": None})}
    )
    with pytest.raises(IndexManifestReadError, match="no orientation map"):
        load_orientation_map(tmp_path, manifest=no_orientation)

    wrong_graph = manifest.model_copy(
        update={
            "artifacts": artifacts.model_copy(
                update={"relationship_graph": artifacts.orientation_map}
            )
        }
    )
    with pytest.raises(IndexManifestReadError, match="graph does not match"):
        load_relationship_graph(tmp_path, manifest=wrong_graph)
    wrong_orientation = manifest.model_copy(
        update={
            "artifacts": artifacts.model_copy(
                update={"orientation_map": artifacts.relationship_graph}
            )
        }
    )
    with pytest.raises(IndexManifestReadError, match="orientation map does not match"):
        load_orientation_map(tmp_path, manifest=wrong_orientation)

    stale = manifest.model_copy(
        update={
            "build": manifest.build.model_copy(
                update={"source_snapshot_digest": "f" * 64}
            )
        }
    )
    with pytest.raises(IndexManifestReadError, match="graph is stale"):
        load_relationship_graph(tmp_path, manifest=stale)
    with pytest.raises(IndexManifestReadError, match="orientation map is stale"):
        load_orientation_map(tmp_path, manifest=stale)

    with pytest.raises(IndexManifestReadError, match="path is absent"):
        load_file_code_map(tmp_path, "missing.py", manifest=manifest)
    first, second = manifest.files
    wrong_state = first.model_copy(
        update={
            "record_location": second.record_location,
            "record_sha256": second.record_sha256,
        }
    )
    wrong_code_map = manifest.model_copy(update={"files": (wrong_state, second)})
    with pytest.raises(IndexManifestReadError, match="identity"):
        load_file_code_map(tmp_path, first.path, manifest=wrong_code_map)


def test_structural_builder_validates_lock_snapshot_and_cancellation(
    tmp_path: Path,
) -> None:
    initialize_index(tmp_path)
    snapshot = scan_repository(tmp_path)
    with acquire_index_lock(tmp_path, "validation") as lock:
        with pytest.raises(ValueError, match="ProjectSnapshot"):
            build_structural_index("bad", lock)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="positive integer"):
            build_structural_index(snapshot, lock, max_source_bytes=0)
        cancellation = asyncio.Event()
        cancellation.set()
        with pytest.raises(asyncio.CancelledError):
            build_structural_index(snapshot, lock, cancellation=cancellation)

    other = tmp_path / "other"
    other.mkdir()
    other_snapshot = scan_repository(other)
    with (
        acquire_index_lock(tmp_path, "wrong-root") as lock,
        pytest.raises(ValueError, match="locked repository"),
    ):
        build_structural_index(other_snapshot, lock)


def test_empty_relationship_graph_and_orientation_are_valid() -> None:
    graph = build_relationship_graph((), DIGEST)
    orientation = build_orientation_map((), graph)

    assert graph.nodes == ()
    assert graph.edges == ()
    assert graph.file_metrics == ()
    assert orientation.files == ()
    assert orientation.modules == ()


def test_graph_projection_ids_and_module_files_must_be_canonical() -> None:
    node = RelationshipGraphNode(
        node_id="file:module.py", kind="file", path="module.py"
    )
    metric = FileGraphMetrics(
        path="module.py",
        pagerank=1.0,
        normalized_centrality=1.0,
        fan_in=0,
        fan_out=0,
    )
    edge = RelationshipGraphEdge(
        edge_id=DIGEST,
        kind="reference",
        source_node_id=node.node_id,
        target_node_id=node.node_id,
        source_file_path="module.py",
        provenance="model-inferred",
        detection_method="test",
    )
    with pytest.raises(ValidationError, match="edges must be unique"):
        RelationshipGraph(
            source_snapshot_digest=DIGEST,
            nodes=(node,),
            edges=(edge, edge),
            file_metrics=(metric,),
        )
    with pytest.raises(ValidationError, match="metrics must be unique"):
        RelationshipGraph(
            source_snapshot_digest=DIGEST,
            nodes=(node,),
            edges=(),
            file_metrics=(metric, metric),
        )
    with pytest.raises(ValidationError, match="module files must be unique"):
        OrientationModule(module="root", files=("z.py", "a.py"), centrality=0.0)
