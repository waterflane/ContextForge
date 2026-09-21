import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from contextforge.application import IndexBuildReport, build_repository_index
from contextforge.intelligence import (
    CandidateEvidenceRange,
    CandidateGraphNeighbor,
    ContextPlanningMode,
    EvidencePlanningError,
    FileGraphMetrics,
    RelationshipGraph,
    RelationshipGraphEdge,
    RelationshipGraphNode,
    RepresentationCosts,
    RetrievalDocument,
    RetrievalField,
    RetrievalIndex,
    RetrievalIndexShardManifest,
    SourceRange,
    build_retrieval_index,
    load_file_code_map,
    load_retrieval_index,
    retrieve_context_candidates,
)
from contextforge.models import FakeModelProvider, ProviderConfiguration


def _write(root: Path, path: str, content: str) -> None:
    destination = root.joinpath(*path.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8", newline="")


def _build(root: Path) -> IndexBuildReport:
    return asyncio.run(
        build_repository_index(
            root,
            provider=None,
            provider_configuration=None,
        )
    )


def _provider(responder: object) -> FakeModelProvider:
    configuration = ProviderConfiguration(
        provider_id="fake",
        endpoint="http://127.0.0.1:1",
        model_id="retrieval-test",
        retry_limit=0,
        max_json_repair_attempts=0,
    )
    return FakeModelProvider(configuration, responder=responder)  # type: ignore[arg-type]


def test_exact_symbol_precedes_graph_related_approximate_candidates(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "src/service.py",
        "def handle_request(value: str) -> str:\n    return value.upper()\n",
    )
    _write(
        tmp_path,
        "src/app.py",
        "from .service import handle_request\n\ndef start():\n"
        "    return handle_request('ready')\n",
    )
    _write(
        tmp_path,
        "tests/test_service.py",
        "from src.service import handle_request\n\ndef test_handle():\n"
        "    assert handle_request('x') == 'X'\n",
    )
    report = _build(tmp_path)

    result = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "change handle_request startup flow",
            manifest=report.manifest,
        )
    )

    assert result.candidates[0].path == "src/service.py"
    assert result.candidates[0].exact_group == "exact_symbol"
    assert "handle_request" in result.candidates[0].matched_symbols
    assert result.candidates[0].evidence_ranges
    app = next(item for item in result.candidates if item.path == "src/app.py")
    assert app.exact_group == "exact_source_identifier"
    assert app.evidence_ranges
    assert any(item.path == "src/service.py" for item in app.graph_neighbors)
    assert report.manifest.artifacts.semantic_retrieval is not None


def test_working_set_and_diff_boosts_are_deterministic(tmp_path: Path) -> None:
    _write(tmp_path, "alpha.py", "def alpha():\n    return 1\n")
    _write(tmp_path, "beta.py", "def beta():\n    return 2\n")
    _write(tmp_path, "gamma.py", "def gamma():\n    return 3\n")
    report = _build(tmp_path)

    first = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "unrelated migration concern",
            manifest=report.manifest,
            working_set=("gamma.py",),
            diff_paths=("beta.py",),
        )
    )
    second = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "unrelated migration concern",
            manifest=report.manifest,
            working_set=("gamma.py",),
            diff_paths=("beta.py",),
        )
    )

    assert [item.path for item in first.candidates[:2]] == ["gamma.py", "beta.py"]
    by_path = {item.path: item for item in first.candidates}
    assert "working-set" in by_path["gamma.py"].provenance
    assert "current-diff" in by_path["beta.py"].provenance
    assert first == second
    assert first.provider_calls == 0


def test_unicode_identifiers_and_exact_paths_are_strict_groups(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/обработка.py",
        "def обработать(значение: str) -> str:\n    return значение\n",
    )
    _write(tmp_path, "README.md", "# Руководство\n\nОписание обработки.\n")
    report = _build(tmp_path)

    symbol = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "исправить обработать",
            manifest=report.manifest,
        )
    )
    path = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "review src/обработка.py",
            manifest=report.manifest,
        )
    )

    assert symbol.candidates[0].path == "src/обработка.py"
    assert symbol.candidates[0].exact_group == "exact_symbol"
    assert path.candidates[0].exact_group == "exact_path"


def test_rerank_rejects_unknown_ids_after_one_repair(tmp_path: Path) -> None:
    _write(tmp_path, "one.py", "def one():\n    return 1\n")
    _write(tmp_path, "two.py", "def two():\n    return 2\n")
    report = _build(tmp_path)
    provider = _provider(
        lambda request, call: json.dumps(
            {
                "schema_version": 1,
                "ordered": [
                    {
                        "candidate_id": "candidate-not-supplied",
                        "representation": "full",
                    }
                ],
            }
        )
    )

    result = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "change functions",
            manifest=report.manifest,
            provider=provider,
            rerank=True,
        )
    )

    assert result.reranked is False
    assert result.provider_calls == 2
    assert provider.call_count == 2
    assert result.diagnostics == ("invalid_rerank_deterministic_fallback",)
    assert all(
        item.candidate_id != "candidate-not-supplied" for item in result.candidates
    )


def test_grounded_semantics_supply_ranked_concepts_and_ranges(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "handler.py",
        "def handle(request: str) -> str:\n    return request.strip()\n",
    )
    provider = _provider(
        lambda request, call: json.dumps(
            {
                "schema_version": 1,
                "synopsis": {
                    "text": "Normalizes inbound requests.",
                    "evidence_ids": ["symbol:0000"],
                },
                "concepts": [
                    {
                        "text": "request normalization",
                        "evidence_ids": ["symbol:0000"],
                    }
                ],
                "responsibilities": [],
                "key_symbols": [
                    {"evidence_id": "symbol:0000", "summary": "Request handler"}
                ],
                "side_effects": [],
                "profile_facts": {},
            }
        )
    )
    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
        )
    )

    result = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "improve request normalization",
            manifest=report.manifest,
        )
    )

    candidate = result.candidates[0]
    assert candidate.path == "handler.py"
    assert candidate.matched_concepts == ("request normalization",)
    assert any(item.strength == "grounded" for item in candidate.evidence_ranges)
    assert "grounded-semantic-card" in candidate.provenance
    assert candidate.estimated_cost.summary is not None
    assert candidate.estimated_cost.slice is not None


def test_valid_rerank_can_only_reorder_and_represent_supplied_candidates(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "one.py", "def one():\n    return 1\n")
    _write(tmp_path, "two.py", "def two():\n    return 2\n")
    report = _build(tmp_path)

    def respond(request: object, call: int) -> str:
        del call
        facts = request.trusted_code_map_facts  # type: ignore[attr-defined]
        identifiers = [item["candidate_id"] for item in facts["candidates"]]
        return json.dumps(
            {
                "schema_version": 1,
                "ordered": [
                    {"candidate_id": item, "representation": "map"}
                    for item in reversed(identifiers)
                ],
            }
        )

    provider = _provider(respond)
    deterministic = asyncio.run(
        retrieve_context_candidates(tmp_path, "functions", manifest=report.manifest)
    )
    reranked = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "functions",
            manifest=report.manifest,
            provider=provider,
            rerank=True,
        )
    )

    assert reranked.reranked is True
    assert reranked.provider_calls == 1
    assert [item.candidate_id for item in reranked.candidates] == list(
        reversed([item.candidate_id for item in deterministic.candidates])
    )
    assert all(item.suggested_representation == "map" for item in reranked.candidates)


def test_retrieval_schemas_reject_noncanonical_postings() -> None:
    digest = "0" * 64
    with pytest.raises(ValidationError, match="positive and canonical"):
        RetrievalField(name="path", length=1, terms={"term": 0})
    with pytest.raises(ValidationError, match="positive and canonical"):
        RetrievalField(name="path", length=2, terms={"z": 1, "a": 1})
    field_values = tuple(
        RetrievalField(name=name, length=0, terms={})  # type: ignore[arg-type]
        for name in ("path", "symbols", "source_identifiers", "grounded_semantics")
    )
    with pytest.raises(ValidationError, match="unique and canonical"):
        RetrievalDocument(
            path="file.py",
            source_sha256=digest,
            fields=field_values,
            symbols=("z", "a"),
        )
    with pytest.raises(ValidationError, match="canonical weighted order"):
        RetrievalDocument(
            path="file.py",
            source_sha256=digest,
            fields=tuple(reversed(field_values)),
        )
    empty = build_retrieval_index((), (), digest)
    assert empty.document_count == 0
    assert all(value == 0 for value in empty.average_field_lengths.values())
    with pytest.raises(ValidationError, match="invalid field order"):
        RetrievalIndex(
            source_snapshot_digest=digest,
            document_count=0,
            documents=(),
            document_frequencies={"path": {}},
            average_field_lengths=dict(empty.average_field_lengths),
        )
    with pytest.raises(ValidationError, match="unique and canonical"):
        RetrievalIndex(
            source_snapshot_digest=digest,
            document_count=2,
            documents=(
                RetrievalDocument(
                    path="file.py",
                    source_sha256=digest,
                    fields=field_values,
                ),
                RetrievalDocument(
                    path="file.py",
                    source_sha256=digest,
                    fields=field_values,
                ),
            ),
            document_frequencies=dict(empty.document_frequencies),
            average_field_lengths=dict(empty.average_field_lengths),
        )
    source_range = SourceRange(start_line=1, start_column=0, end_line=1, end_column=1)
    with pytest.raises(ValidationError, match="portable relative"):
        CandidateEvidenceRange(
            path="../outside.py",
            source_range=source_range,
            strength="verified",
        )
    with pytest.raises(ValidationError, match="portable relative"):
        CandidateGraphNeighbor(
            path="/absolute.py",
            distance=1,
            relationship_kinds=(),
            provenance=(),
        )
    costs = RepresentationCosts(map=1, full=1)
    assert costs.summary is None and costs.slice is None


def test_retrieval_validates_task_and_limit_before_reading_index(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        asyncio.run(retrieve_context_candidates(tmp_path, ""))
    with pytest.raises(ValueError, match="between 1 and 1000"):
        asyncio.run(retrieve_context_candidates(tmp_path, "task", limit=0))


def test_structural_generation_retrieval_needs_no_semantic_cards(
    tmp_path: Path,
) -> None:
    _write(
        tmp_path,
        "service.py",
        "class Service:\n    def execute(self) -> None:\n        pass\n",
    )
    report = _build(tmp_path)

    result = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "change service.Service.execute",
            manifest=report.structural.manifest,
        )
    )

    candidate = result.candidates[0]
    assert candidate.exact_group == "exact_qualified_symbol"
    assert candidate.synopsis == "Structural map for service.py."
    assert candidate.estimated_cost.summary is None
    assert candidate.matched_concepts == ()


def test_retrieval_rejects_missing_and_stale_generation_artifacts(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "service.py", "def serve():\n    return None\n")
    report = _build(tmp_path)
    missing = report.manifest.model_copy(
        update={
            "artifacts": report.manifest.artifacts.model_copy(
                update={"semantic_retrieval": None, "structural_retrieval": None}
            )
        }
    )
    with pytest.raises(ValueError, match="no persisted retrieval"):
        asyncio.run(retrieve_context_candidates(tmp_path, "serve", manifest=missing))

    stale = report.manifest.model_copy(
        update={
            "build": report.manifest.build.model_copy(
                update={"source_snapshot_digest": "f" * 64}
            )
        }
    )
    with pytest.raises(ValueError, match="stale"):
        asyncio.run(retrieve_context_candidates(tmp_path, "serve", manifest=stale))


def test_rerank_recovers_once_from_provider_failure(tmp_path: Path) -> None:
    _write(tmp_path, "service.py", "def serve():\n    return None\n")
    report = _build(tmp_path)

    def respond(request: object, call: int) -> str | RuntimeError:
        if call == 0:
            return RuntimeError("transient")
        facts = request.trusted_code_map_facts  # type: ignore[attr-defined]
        identifier = facts["candidates"][0]["candidate_id"]
        return json.dumps(
            {
                "schema_version": 1,
                "ordered": [{"candidate_id": identifier, "representation": "slice"}],
            }
        )

    provider = _provider(respond)
    result = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "serve",
            manifest=report.manifest,
            provider=provider,
            rerank=True,
        )
    )

    assert result.reranked is True
    assert result.provider_calls == 2
    assert result.candidates[0].suggested_representation == "slice"


def test_source_identifier_and_two_hop_graph_signals(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "entry.py",
        "from middle import forward\n\ndef start():\n    return forward()\n",
    )
    _write(
        tmp_path,
        "middle.py",
        "from settings_reader import load_setting\n\ndef forward():\n"
        "    return load_setting()\n",
    )
    _write(
        tmp_path,
        "settings_reader.py",
        "import os\n\ndef load_setting():\n    return os.getenv('SPECIAL_KEY')\n",
    )
    report = _build(tmp_path)

    identifier = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "change SPECIAL_KEY",
            manifest=report.manifest,
        )
    )
    flow = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "trace load_setting",
            manifest=report.manifest,
        )
    )

    assert identifier.candidates[0].exact_group == "exact_source_identifier"
    middle = next(item for item in flow.candidates if item.path == "middle.py")
    assert middle.exact_group == "exact_source_identifier"
    entry = next(item for item in flow.candidates if item.path == "entry.py")
    assert "graph-1-hop" in entry.provenance


def test_model_inferred_neighbors_do_not_create_retrieval_distance() -> None:
    from contextforge.intelligence import retrieval as retrieval_module

    digest = "0" * 64
    nodes = (
        RelationshipGraphNode(node_id="file:source.py", kind="file", path="source.py"),
        RelationshipGraphNode(node_id="file:target.py", kind="file", path="target.py"),
    )
    metrics = tuple(
        FileGraphMetrics(
            path=path,
            pagerank=0.5,
            normalized_centrality=0.0,
            fan_in=0,
            fan_out=0,
        )
        for path in ("source.py", "target.py")
    )
    inferred_edge = RelationshipGraphEdge(
        edge_id="1" * 64,
        kind="reference",
        source_node_id="file:source.py",
        target_node_id="file:target.py",
        source_file_path="source.py",
        provenance="model-inferred",
        detection_method="semantic_card_closed_candidate",
    )
    inferred_graph = RelationshipGraph(
        source_snapshot_digest=digest,
        nodes=nodes,
        edges=(inferred_edge,),
        file_metrics=metrics,
    )

    assert retrieval_module._graph_distances(inferred_graph, {"source.py"}) == {
        "source.py": 0
    }
    inferred_neighbor = retrieval_module._candidate_neighbors(inferred_graph)[
        "source.py"
    ][0]
    assert inferred_neighbor.path == "target.py"
    assert inferred_neighbor.provenance == ("model-inferred",)

    structural_graph = inferred_graph.model_copy(
        update={
            "edges": (
                inferred_edge.model_copy(
                    update={"provenance": "best-effort-structural"}
                ),
            )
        }
    )
    assert (
        retrieval_module._graph_distances(structural_graph, {"source.py"})["target.py"]
        == 1
    )


def test_rerank_returns_deterministic_result_after_two_provider_failures(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "service.py", "def serve():\n    return None\n")
    report = _build(tmp_path)
    provider = _provider(lambda request, call: RuntimeError("offline"))

    result = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "serve",
            manifest=report.manifest,
            provider=provider,
            rerank=True,
        )
    )

    assert result.reranked is False
    assert result.provider_calls == 2
    assert result.diagnostics == ("rerank_failed_deterministic_fallback",)


def test_evidence_planner_selects_only_supplied_ranges(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "service.py",
        "def handle_request(value: str) -> str:\n    return value.upper()\n",
    )
    report = _build(tmp_path)

    def respond(request: object, call: int) -> str:
        del call
        facts = request.trusted_code_map_facts  # type: ignore[attr-defined]
        assert facts["limits"] == {"max_files": 2, "max_ranges_per_file": 1}
        candidate = facts["candidates"][0]
        assert candidate["representation_costs"]["map"] > 0
        return json.dumps(
            {
                "schema_version": 1,
                "selected": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "evidence_ids": [candidate["evidence"][0]["evidence_id"]],
                        "representation": "slice",
                    }
                ],
                "sufficiency": "sufficient",
            }
        )

    result = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "change handle_request",
            manifest=report.manifest,
            provider=_provider(respond),
            planning_mode=ContextPlanningMode.AUTO,
            planning_max_files=2,
            planning_max_ranges_per_file=1,
        )
    )

    assert result.evidence_plan is not None
    assert result.evidence_plan.diagnostics.status == "planned"
    assert result.evidence_plan.items[0].evidence_ids
    supplied = {
        item.evidence_id
        for item in result.candidates[0].evidence_ranges
        if item.evidence_id is not None
    }
    assert set(result.evidence_plan.items[0].evidence_ids) <= supplied


def test_evidence_planner_retains_only_supplied_role_bindings(tmp_path: Path) -> None:
    _write(tmp_path, "main.py", "def startup() -> None:\n    return None\n")
    report = _build(tmp_path)

    def respond(request: object, call: int) -> str:
        del call
        facts = request.trusted_code_map_facts  # type: ignore[attr-defined]
        candidate = facts["candidates"][0]
        role_ids = {item["role_id"] for item in facts["task_evidence_roles"]}
        assert {"entrypoint", "implementation"} <= role_ids
        return json.dumps(
            {
                "schema_version": 1,
                "selected": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "evidence_ids": [candidate["evidence"][0]["evidence_id"]],
                        "representation": "slice",
                    }
                ],
                "role_bindings": [
                    {
                        "role_id": "entrypoint",
                        "candidate_id": candidate["candidate_id"],
                        "evidence_ids": [candidate["evidence"][0]["evidence_id"]],
                    },
                    {
                        "role_id": "unknown",
                        "candidate_id": candidate["candidate_id"],
                        "evidence_ids": [candidate["evidence"][0]["evidence_id"]],
                    },
                ],
                "sufficiency": "sufficient",
            }
        )

    result = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "Review startup implementation.",
            manifest=report.manifest,
            provider=_provider(respond),
            planning_mode=ContextPlanningMode.AUTO,
        )
    )

    assert result.evidence_plan is not None
    assert [
        (item.role_id, item.source) for item in result.evidence_plan.role_bindings
    ] == [("entrypoint", "planner")]
    assert result.coverage_ledger is not None
    assert "entrypoint" in result.coverage_ledger.covered_role_ids


def test_planner_does_not_advertise_full_for_large_file(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "large.py",
        "def serve():\n    return 1\n"
        + "".join(f"# filler {line}\n" for line in range(1, 220)),
    )
    report = _build(tmp_path)

    def respond(request: object, call: int) -> str:
        del call
        facts = request.trusted_code_map_facts  # type: ignore[attr-defined]
        candidate = facts["candidates"][0]
        assert "full" not in candidate["available_representations"]
        return json.dumps(
            {
                "schema_version": 1,
                "actions": [
                    {
                        "action": "finalize",
                        "selected": [
                            {
                                "candidate_id": candidate["candidate_id"],
                                "representation": "map",
                            }
                        ],
                        "sufficiency": "sufficient",
                    }
                ],
            }
        )

    result = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "serve",
            manifest=report.manifest,
            provider=_provider(respond),
            planning_mode="auto",
        )
    )

    assert result.evidence_plan is not None
    assert result.evidence_plan.items[0].representation == "map"


def test_agentic_planner_discovers_candidate_outside_initial_pool(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "decoy.py", "def unrelated():\n    return 'decoy'\n")
    _write(
        tmp_path,
        "lifecycle.py",
        "def shutdown_worker():\n    return 'stopped'\n",
    )
    report = _build(tmp_path)

    def respond(request: object, call: int) -> str:
        facts = request.trusted_code_map_facts  # type: ignore[attr-defined]
        if call == 0:
            assert len(facts["candidates"]) == 1
            return json.dumps(
                {
                    "schema_version": 1,
                    "actions": [
                        {
                            "action": "symbol",
                            "identifier": "shutdown_worker",
                            "limit": 4,
                        }
                    ],
                }
            )
        candidate = next(
            item for item in facts["candidates"] if item["path"] == "lifecycle.py"
        )
        return json.dumps(
            {
                "schema_version": 1,
                "actions": [
                    {
                        "action": "finalize",
                        "selected": [
                            {
                                "candidate_id": candidate["candidate_id"],
                                "representation": "map",
                            }
                        ],
                        "sufficiency": "sufficient",
                    }
                ],
            }
        )

    result = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "explain unrelated",
            manifest=report.manifest,
            provider=_provider(respond),
            planning_mode="auto",
            planning_max_candidates=1,
        )
    )

    assert result.evidence_plan is not None
    assert result.evidence_plan.items[0].path == "lifecycle.py"
    assert result.evidence_plan.diagnostics.rounds == 2
    assert result.provider_calls == 2


def test_agentic_planner_combines_search_graph_and_map_expansion(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "helper.py", "def support():\n    return 'ready'\n")
    _write(
        tmp_path,
        "app.py",
        "from helper import support\n\ndef start():\n    return support()\n",
    )
    _write(tmp_path, "flow.py", "def hidden_flow():\n    return 'complete'\n")
    report = _build(tmp_path)

    def respond(request: object, call: int) -> str:
        facts = request.trusted_code_map_facts  # type: ignore[attr-defined]
        if call == 0:
            initial = facts["candidates"][0]
            module = facts["modules"][0]
            return json.dumps(
                {
                    "schema_version": 1,
                    "actions": [
                        {"action": "search", "query": "hidden_flow", "limit": 4},
                        {
                            "action": "graph",
                            "candidate_id": initial["candidate_id"],
                            "hops": 2,
                        },
                        {"action": "map", "module_id": module["module_id"]},
                    ],
                }
            )
        candidate = next(
            item for item in facts["candidates"] if item["path"] == "flow.py"
        )
        assert {item["action"] for item in facts["completed_actions"]} == {
            "graph",
            "map",
        }
        return json.dumps(
            {
                "schema_version": 1,
                "actions": [
                    {
                        "action": "finalize",
                        "selected": [
                            {
                                "candidate_id": candidate["candidate_id"],
                                "representation": "map",
                            }
                        ],
                        "sufficiency": "sufficient",
                    }
                ],
            }
        )

    result = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "start",
            manifest=report.manifest,
            provider=_provider(respond),
            planning_mode="auto",
            planning_max_candidates=1,
        )
    )

    assert result.evidence_plan is not None
    assert result.evidence_plan.items[0].path == "flow.py"
    assert result.evidence_plan.diagnostics.rounds == 2
    assert [item.stage for item in result.coverage_history] == [
        "retrieval",
        "action",
        "action",
        "action",
        "plan",
    ]


def test_agentic_planner_shrinks_each_round_to_provider_context(tmp_path: Path) -> None:
    for index in range(20):
        _write(
            tmp_path,
            f"module_{index:02d}/service.py",
            f"def service_{index:02d}():\n    return {index}\n",
        )
    report = _build(tmp_path)
    observed_candidate_counts: list[int] = []
    observed_module_counts: list[int] = []

    def respond(request: object, call: int) -> str:
        del call
        facts = request.trusted_code_map_facts  # type: ignore[attr-defined]
        observed_candidate_counts.append(len(facts["candidates"]))
        observed_module_counts.append(len(facts["modules"]))
        candidate = facts["candidates"][0]
        return json.dumps(
            {
                "schema_version": 1,
                "actions": [
                    {
                        "action": "finalize",
                        "selected": [
                            {
                                "candidate_id": candidate["candidate_id"],
                                "representation": "map",
                            }
                        ],
                        "sufficiency": "sufficient",
                    }
                ],
            }
        )

    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="http://127.0.0.1:1",
            model_id="retrieval-tight-context",
            context_window=3_000,
            retry_limit=0,
            max_json_repair_attempts=0,
        ),
        responder=respond,
    )
    result = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "service",
            manifest=report.manifest,
            provider=provider,
            planning_mode="auto",
        )
    )

    assert result.evidence_plan is not None
    assert observed_candidate_counts
    assert 0 < observed_candidate_counts[0] < 20
    assert 0 <= observed_module_counts[0] < 20


def test_agentic_planner_rejects_unsupplied_graph_target(tmp_path: Path) -> None:
    _write(tmp_path, "service.py", "def serve():\n    return None\n")
    report = _build(tmp_path)
    provider = _provider(
        lambda request, call: json.dumps(
            {
                "schema_version": 1,
                "actions": [
                    {"action": "graph", "candidate_id": "not-supplied", "hops": 2}
                ],
            }
        )
    )

    result = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "serve",
            manifest=report.manifest,
            provider=provider,
            planning_mode="auto",
        )
    )

    assert result.evidence_plan is None
    assert result.diagnostics == ("planner_unknown_action_target",)
    assert result.provider_calls == 1


def test_agentic_planner_rejects_unknown_module(tmp_path: Path) -> None:
    _write(tmp_path, "service.py", "def serve():\n    return None\n")
    report = _build(tmp_path)
    provider = _provider(
        lambda request, call: json.dumps(
            {
                "schema_version": 1,
                "actions": [{"action": "map", "module_id": "missing-module"}],
            }
        )
    )

    result = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "serve",
            manifest=report.manifest,
            provider=provider,
            planning_mode="auto",
        )
    )

    assert result.evidence_plan is None
    assert result.diagnostics == ("planner_unknown_module",)
    assert result.provider_calls == 1


def test_planner_previews_sample_late_evidence_and_bound_large_declarations() -> None:
    from contextforge.intelligence import retrieval as retrieval_module

    evidence = tuple(
        CandidateEvidenceRange(
            path="large.py",
            source_range=SourceRange(
                start_line=line,
                start_column=0,
                end_line=line,
                end_column=1,
            ),
            evidence_id=f"evidence-{line}",
            strength="verified",
        )
        for line in range(1, 15)
    )

    ordered = retrieval_module._planner_evidence_order(evidence)
    assert tuple(item.source_range.start_line for item in ordered[:8]) == (
        1,
        3,
        5,
        7,
        8,
        10,
        12,
        14,
    )
    preview = retrieval_module._planner_preview_block(
        [f"line {line}" for line in range(1, 146)],
        SourceRange(start_line=1, start_column=0, end_line=145, end_column=1),
    )
    assert "line 1" in preview
    assert "line 73" in preview
    assert "line 109" in preview
    assert "line 145" in preview
    assert len(preview.splitlines()) < 50


def test_required_evidence_planning_fails_without_provider(tmp_path: Path) -> None:
    _write(tmp_path, "service.py", "def serve():\n    return None\n")
    report = _build(tmp_path)

    with pytest.raises(EvidencePlanningError):
        asyncio.run(
            retrieve_context_candidates(
                tmp_path,
                "serve",
                manifest=report.manifest,
                planning_mode="required",
            )
        )


def test_retrieval_persists_safe_positional_structural_postings(tmp_path: Path) -> None:
    _write(tmp_path, "dependency.py", "class ServiceClient:\n    pass\n")
    _write(
        tmp_path,
        "app.py",
        "from dependency import ServiceClient\n\n"
        "def start():\n    client = ServiceClient()\n    return client\n",
    )
    report = _build(tmp_path)
    code_maps = tuple(
        load_file_code_map(tmp_path, state.path, manifest=report.manifest)
        for state in report.manifest.files
    )
    index = build_retrieval_index(
        code_maps, (), report.manifest.build.source_snapshot_digest
    )
    document = next(item for item in index.documents if item.path == "app.py")

    assert {item.fact_kind for item in document.positional_postings} >= {
        "declaration",
        "import",
        "call",
        "reference",
    }
    assert all(
        item.evidence_id.startswith("structural-")
        for item in document.positional_postings
    )
    serialized = document.model_dump_json()
    assert "ServiceClient" in serialized
    assert "pass" not in {item.identifier for item in document.positional_postings}


def test_positional_postings_retain_at_most_eight_positions_per_identifier_kind(
    tmp_path: Path,
) -> None:
    calls = "".join("    missing()\n" for _ in range(20))
    _write(tmp_path, "app.py", f"def run():\n{calls}")
    report = _build(tmp_path)
    code_map = load_file_code_map(tmp_path, "app.py", manifest=report.manifest)
    index = build_retrieval_index(
        (code_map,), (), report.manifest.build.source_snapshot_digest
    )

    missing_calls = [
        item
        for item in index.documents[0].positional_postings
        if item.fact_kind == "call" and item.identifier == "missing"
    ]
    assert len(missing_calls) == 8


def test_exact_identifier_restores_evidence_beyond_bounded_postings(
    tmp_path: Path,
) -> None:
    noisy_references = "\n".join(f"    helper_{index:03d}" for index in range(180))
    _write(
        tmp_path,
        "large.py",
        f"def route(planning_mode):\n{noisy_references}\n    return planning_mode\n",
    )
    report = _build(tmp_path)
    code_map = load_file_code_map(tmp_path, "large.py", manifest=report.manifest)
    index = build_retrieval_index(
        (code_map,), (), report.manifest.build.source_snapshot_digest
    )

    assert "planning_mode" in index.exact_identifier_documents
    assert "planning_mode" not in index.documents[0].source_identifiers

    result = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "Explain planning_mode",
            manifest=report.manifest,
        )
    )

    candidate = result.candidates[0]
    assert candidate.path == "large.py"
    assert candidate.exact_group == "exact_source_identifier"
    assert "planning_mode" in candidate.matched_symbols
    assert candidate.evidence_ranges
    assert any(item.source_range.end_line == 182 for item in candidate.evidence_ranges)


def test_code_shaped_identifier_suppresses_incidental_exact_prose_symbols(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "noise.py", "def search():\n    return None\n")
    _write(
        tmp_path,
        "target.py",
        "def route(planning_mode):\n    return planning_mode\n",
    )
    report = _build(tmp_path)

    result = asyncio.run(
        retrieve_context_candidates(
            tmp_path,
            "Search where planning_mode is applied",
            manifest=report.manifest,
        )
    )

    assert result.candidates[0].path == "target.py"
    assert result.candidates[0].exact_group == "exact_source_identifier"
    noise = next(item for item in result.candidates if item.path == "noise.py")
    assert noise.exact_group == "approximate"


def test_retrieval_uses_digest_bound_grounding_without_reopening_cards(
    tmp_path: Path,
) -> None:
    _write(tmp_path, "service.py", "def serve():\n    return None\n")
    report = _build(tmp_path)
    state = report.manifest.files[0].model_copy(
        update={
            "interpretation_record_location": "files/missing.interpretation.json",
            "interpretation_record_sha256": "f" * 64,
            "semantic_status": "complete",
        }
    )
    manifest = report.manifest.model_copy(update={"files": (state,)})

    result = asyncio.run(
        retrieve_context_candidates(tmp_path, "serve", manifest=manifest)
    )

    assert result.candidates[0].synopsis.startswith("Source file service.py")
    assert "grounded-semantic-card" in result.candidates[0].provenance


def test_retrieval_cache_revalidates_digest_bound_shards(tmp_path: Path) -> None:
    _write(tmp_path, "service.py", "def serve():\n    return None\n")
    report = _build(tmp_path)
    reference = report.manifest.artifacts.semantic_retrieval
    assert reference is not None

    first = load_retrieval_index(tmp_path, reference, manifest=report.manifest)
    second = load_retrieval_index(tmp_path, reference, manifest=report.manifest)
    assert second is first

    generation = (
        tmp_path
        / ".contextforge"
        / "index"
        / "generations"
        / report.manifest.generation_id
    )
    header = RetrievalIndexShardManifest.model_validate_json(
        (generation / reference.location).read_bytes()
    )
    shard = generation / header.document_shards[0].artifact.location
    shard.write_bytes(shard.read_bytes() + b" ")

    with pytest.raises(ValueError, match="shard digest"):
        load_retrieval_index(tmp_path, reference, manifest=report.manifest)


def test_retrieval_internal_guards_and_tokenization() -> None:
    from contextforge.intelligence import retrieval as retrieval_module

    assert retrieval_module._tokens("HTTPServer2 naïve") == (
        "httpserver2",
        "naïve",
        "http",
        "server",
        "2",
        "na",
        "ve",
    )
    assert retrieval_module._exact_text("run runtime", "run") is True
    assert retrieval_module._exact_text("runtime", "run") is False
    assert retrieval_module._exact_text("вызвать запуск", "запуск") is True
    with pytest.raises(TypeError, match="relationship graph"):
        retrieval_module._rank_candidates(
            "task",
            build_retrieval_index((), (), "0" * 64),
            object(),
            working_set=(),
            diff_paths=(),
        )
