import asyncio
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from contextforge.application import build_repository_index
from contextforge.intelligence import (
    CandidateEvidenceRange,
    CandidateGraphNeighbor,
    RepresentationCosts,
    RetrievalDocument,
    RetrievalField,
    RetrievalIndex,
    SourceRange,
    build_retrieval_index,
    retrieve_context_candidates,
)
from contextforge.models import FakeModelProvider, ProviderConfiguration


def _write(root: Path, path: str, content: str) -> None:
    destination = root.joinpath(*path.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8", newline="")


def _build(root: Path):
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
    assert "graph-1-hop" in app.provenance
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
    entry = next(item for item in flow.candidates if item.path == "entry.py")
    assert "graph-2-hop" in entry.provenance


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


def test_invalid_semantic_record_is_excluded_from_ranking(tmp_path: Path) -> None:
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

    assert result.candidates[0].synopsis == "Structural map for service.py."


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
    with pytest.raises(TypeError, match="relationship graph"):
        retrieval_module._rank_candidates(  # type: ignore[arg-type]
            "task",
            build_retrieval_index((), (), "0" * 64),
            {},
            {},
            object(),
            working_set=(),
            diff_paths=(),
        )
