import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from contextforge.application import build_repository_index
from contextforge.intelligence import (
    build_retrieval_index,
    load_file_code_map,
    load_relationship_graph,
)
from contextforge.intelligence import semantic_lexicon as lexicon_module
from contextforge.intelligence.file_policy import FILE_POLICY_REGISTRY
from contextforge.intelligence.semantic_lexicon import (
    SemanticContextOverflow,
    SemanticLexiconBudgetExceeded,
    analyze_file_lexicon,
    analyze_file_lexicon_resumable,
)
from contextforge.models import (
    ContextWindowExceededError,
    FakeModelProvider,
    ProviderConfiguration,
    ProviderQuotaError,
    estimate_request_context,
)


@pytest.mark.parametrize(
    "expression",
    ["", " ", " too long ", "word " * 9, "x" * 81, "поиск", "123"],
)
def test_model_search_expressions_are_bounded_printable_ascii(
    expression: str,
) -> None:
    with pytest.raises(ValidationError):
        lexicon_module._RawLexicon.model_validate(
            {
                "functions": [
                    {
                        "symbol_id": "supplied-id",
                        "summary": "Describes the callable.",
                        "expressions": [expression],
                    }
                ]
            }
        )


def test_repeated_model_expressions_do_not_add_search_weight() -> None:
    assert lexicon_module._unique_expressions(
        ("request routing", "Request Routing", "route invocation")
    ) == ("request routing", "route invocation")


def _fixture(root: Path):
    (root / "service.py").write_text(
        "def greet(value: str) -> str:\n    return value.upper()\n",
        encoding="utf-8",
    )
    (root / "app.py").write_text(
        "from service import greet\n\ndef start() -> str:\n    return greet('ready')\n",
        encoding="utf-8",
    )
    report = asyncio.run(
        build_repository_index(root, provider=None, provider_configuration=None)
    )
    maps = {
        path: load_file_code_map(root, path, manifest=report.manifest)
        for path in ("app.py", "service.py")
    }
    graph = load_relationship_graph(root, manifest=report.manifest)
    sources = {path: (root / path).read_text(encoding="utf-8") for path in maps}
    return maps, graph, sources


def _provider(responder, *, context_window: int = 16384):
    return FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="http://127.0.0.1:1",
            model_id="lexicon-test",
            context_window=context_window,
            retry_limit=0,
            max_json_repair_attempts=0,
        ),
        responder=responder,
    )


def _response(request, *, invented: bool = False):
    if request.purpose == "semantic-lexicon-verification":
        return json.dumps(
            {
                "schema_version": 1,
                "accepted_ids": sorted(
                    request.trusted_code_map_facts["proposed_claims"]
                ),
            }
        )
    targets = request.trusted_code_map_facts["target_functions"]
    edges = request.trusted_code_map_facts["outgoing_calls"]
    return json.dumps(
        {
            "schema_version": 1,
            "functions": [
                {
                    "symbol_id": "invented" if invented else item["symbol_id"],
                    "summary": "Starts the greeting flow",
                    "expressions": ["greeting entrypoint"],
                }
                for item in targets
            ],
            "calls": [
                {"edge_id": item["edge_id"], "expressions": ["greeting call"]}
                for item in edges
            ],
        }
    )


def test_full_file_and_direct_callee_code_are_sent(tmp_path: Path) -> None:
    maps, graph, sources = _fixture(tmp_path)
    requests = []

    def responder(request, call):
        requests.append(request)
        return _response(request)

    provider = _provider(responder)
    lexicon = asyncio.run(
        analyze_file_lexicon(
            provider, maps["app.py"], sources["app.py"], graph, maps, sources
        )
    )
    first = requests[0]
    assert first.purpose == "semantic-lexicon"
    assert first.trusted_code_map_facts["context_mode"] == "full_graph"
    assert (
        next(item.text for item in first.untrusted_sources if item.path == "app.py")
        == sources["app.py"]
    )
    assert any(
        item.path == "service.py" and "return value.upper()" in item.text
        for item in first.untrusted_sources
    )
    assert first.trusted_code_map_facts["outgoing_calls"]
    assert len(lexicon.functions) == 1
    assert lexicon.calls


def test_invented_symbol_id_is_rejected(tmp_path: Path) -> None:
    maps, graph, sources = _fixture(tmp_path)
    provider = _provider(lambda request, call: _response(request, invented=True))
    with pytest.raises(ValueError, match="invented"):
        asyncio.run(
            analyze_file_lexicon(
                provider, maps["app.py"], sources["app.py"], graph, maps, sources
            )
        )


def test_runtime_limit_retries_without_callee_code(tmp_path: Path) -> None:
    maps, graph, sources = _fixture(tmp_path)
    requests = []

    def responder(request, call):
        requests.append(request)
        if call == 0:
            return ContextWindowExceededError(server_context_window=8192)
        return _response(request)

    lexicon = asyncio.run(
        analyze_file_lexicon(
            _provider(responder),
            maps["app.py"],
            sources["app.py"],
            graph,
            maps,
            sources,
        )
    )
    assert lexicon.context_mode == "file_only"
    assert lexicon.reported_context_window == 8192
    assert requests[1].trusted_code_map_facts["outgoing_calls"]
    assert [item.path for item in requests[1].untrusted_sources] == ["app.py"]


def test_verification_overflow_splits_claims_without_splitting_file(
    tmp_path: Path,
) -> None:
    maps, graph, sources = _fixture(tmp_path)
    requests = []

    def responder(request, call):
        requests.append(request)
        if request.purpose == "semantic-lexicon-verification":
            proposed = request.trusted_code_map_facts["proposed_claims"]
            if (
                request.trusted_code_map_facts["context_mode"] == "full_graph"
                or len(proposed) > 1
            ):
                return ContextWindowExceededError(server_context_window=8192)
        return _response(request)

    lexicon = asyncio.run(
        analyze_file_lexicon(
            _provider(responder),
            maps["app.py"],
            sources["app.py"],
            graph,
            maps,
            sources,
        )
    )
    verification = [
        request
        for request in requests
        if request.purpose == "semantic-lexicon-verification"
    ]
    assert lexicon.context_mode == "file_only"
    assert lexicon.reported_context_window == 8192
    assert any(
        len(request.trusted_code_map_facts["proposed_claims"]) == 1
        for request in verification
    )
    assert all(
        next(item.text for item in request.untrusted_sources if item.path == "app.py")
        == sources["app.py"]
        for request in verification
    )
    assert lexicon.functions[0].summary == "Starts the greeting flow"
    assert all(not call.expressions for call in lexicon.calls)


def test_short_code_file_without_callable_is_model_eligible(tmp_path: Path) -> None:
    (tmp_path / "constants.py").write_text(
        '"""Module placeholder."""\npass\n', encoding="utf-8"
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    code_map = load_file_code_map(tmp_path, "constants.py", manifest=report.manifest)
    assert not code_map.symbols
    assert not FILE_POLICY_REGISTRY.requires_deterministic_card(code_map)

    def responder(request, call):
        return json.dumps(
            {
                "schema_version": 1,
                "synopsis": {
                    "text": "Module placeholder with pass",
                    "evidence_ids": ["file"],
                },
                "concepts": [{"text": "module placeholder", "evidence_ids": ["file"]}],
                "responsibilities": [],
                "key_symbols": [],
                "side_effects": [],
                "profile_facts": {},
            }
        )

    provider = _provider(responder)
    enriched = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
            semantic_scope="all",
        )
    )
    from contextforge.intelligence import load_semantic_card

    card = load_semantic_card(tmp_path, "constants.py", manifest=enriched.manifest)
    assert card.synopsis.text == "Module placeholder with pass", card.diagnostics


def test_lexicon_respects_shared_request_and_token_budgets(tmp_path: Path) -> None:
    maps, graph, sources = _fixture(tmp_path)
    provider = _provider(lambda request, call: _response(request))
    calls = [0]
    tokens = [0]
    with pytest.raises(SemanticLexiconBudgetExceeded, match="request budget"):
        asyncio.run(
            analyze_file_lexicon(
                provider,
                maps["app.py"],
                sources["app.py"],
                graph,
                maps,
                sources,
                call_counter=calls,
                token_counter=tokens,
                request_budget=1,
            )
        )
    assert calls == [1]
    assert provider.call_count == 1
    assert tokens[0] > 0
    blocked = _provider(lambda request, call: _response(request))
    with pytest.raises(SemanticLexiconBudgetExceeded, match="input-token budget"):
        asyncio.run(
            analyze_file_lexicon(
                blocked,
                maps["app.py"],
                sources["app.py"],
                graph,
                maps,
                sources,
                estimated_input_budget=1,
            )
        )
    assert blocked.call_count == 0


def test_full_file_overflow_is_explicit(tmp_path: Path) -> None:
    maps, graph, sources = _fixture(tmp_path)
    provider = _provider(lambda request, call: _response(request), context_window=1024)
    with pytest.raises(SemanticContextOverflow, match="app.py"):
        asyncio.run(
            analyze_file_lexicon(
                provider, maps["app.py"], sources["app.py"], graph, maps, sources
            )
        )
    assert provider.call_count == 0


def test_enriched_generation_indexes_verified_expressions(tmp_path: Path) -> None:
    _fixture(tmp_path)

    def responder(request, call):
        if request.purpose.startswith("semantic-card"):
            return json.dumps(
                {
                    "schema_version": 1,
                    "synopsis": {
                        "text": "Provides greeting behavior",
                        "evidence_ids": ["file"],
                    },
                    "concepts": [{"text": "greeting", "evidence_ids": ["symbol:0000"]}],
                    "responsibilities": [],
                    "key_symbols": [],
                    "side_effects": [],
                    "profile_facts": {},
                }
            )
        return _response(request)

    provider = _provider(responder)
    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
            semantic_scope="all",
        )
    )
    from contextforge.intelligence import load_retrieval_index, load_semantic_card

    card = load_semantic_card(tmp_path, "app.py", manifest=report.manifest)
    assert card.lexicon is not None
    assert len(card.lexicon.functions) == 1
    reference = report.manifest.artifacts.semantic_retrieval
    assert reference is not None
    index = load_retrieval_index(tmp_path, reference, manifest=report.manifest)
    document = next(item for item in index.documents if item.path == "app.py")
    semantic = next(
        item for item in document.fields if item.name == "grounded_semantics"
    )
    assert "greeting" in semantic.terms
    assert any(item.text == "greeting entrypoint" for item in document.semantic_claims)
    call_claim = next(
        item for item in document.semantic_claims if item.text == "greeting call"
    )
    assert card.lexicon.calls
    assert call_claim.evidence[0].evidence_id == (
        f"lexicon-call:{card.lexicon.calls[0].edge_id}"
    )
    assert call_claim.evidence[0].source_range is not None
    maps = {
        path: load_file_code_map(tmp_path, path, manifest=report.manifest)
        for path in ("app.py", "service.py")
    }
    graph = load_relationship_graph(tmp_path, manifest=report.manifest)
    without_callsite = graph.model_copy(
        update={
            "edges": tuple(
                edge.model_copy(update={"source_range": None})
                if edge.edge_id == card.lexicon.calls[0].edge_id
                else edge
                for edge in graph.edges
            )
        }
    )
    fallback_index = build_retrieval_index(
        tuple(maps.values()),
        (card,),
        report.manifest.build.source_snapshot_digest,
        relationship_graph=without_callsite,
    )
    fallback_document = next(
        item for item in fallback_index.documents if item.path == "app.py"
    )
    fallback_call = next(
        item
        for item in fallback_document.semantic_claims
        if item.text == "greeting call"
    )
    assert fallback_call.evidence[0].source_range is not None
    assert fallback_call.evidence[0].source_range.start_line == 3
    assert fallback_call.evidence[0].source_range.end_line == 4
    no_graph_index = build_retrieval_index(
        tuple(maps.values()),
        (card,),
        report.manifest.build.source_snapshot_digest,
    )
    no_graph_document = next(
        item for item in no_graph_index.documents if item.path == "app.py"
    )
    assert all(
        item.text != "greeting call" for item in no_graph_document.semantic_claims
    )
    assert report.semantic is not None
    assert report.semantic.request_count == provider.call_count


def test_each_batch_receives_full_file_and_complete_function_table(
    tmp_path: Path,
) -> None:
    maps, graph, sources = _fixture(tmp_path)
    del maps, graph, sources
    path = tmp_path / "service.py"
    path.write_text(
        "".join(
            f"def function_{number}() -> int:\n    return {number}\n\n"
            for number in range(5)
        ),
        encoding="utf-8",
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    code_map = load_file_code_map(tmp_path, "service.py", manifest=report.manifest)
    graph = load_relationship_graph(tmp_path, manifest=report.manifest)
    source = path.read_text(encoding="utf-8")
    requests = []

    def responder(request, call):
        requests.append(request)
        return _response(request)

    provider = _provider(responder)
    lexicon = asyncio.run(
        analyze_file_lexicon(
            provider,
            code_map,
            source,
            graph,
            {"service.py": code_map},
            {"service.py": source},
        )
    )
    builds = [request for request in requests if request.purpose == "semantic-lexicon"]
    assert len(builds) == 2
    assert [
        len(request.trusted_code_map_facts["target_functions"]) for request in builds
    ] == [4, 1]
    assert all(
        len(request.trusted_code_map_facts["file_functions"]) == 5 for request in builds
    )
    assert all(request.untrusted_sources[0].text == source for request in requests)
    assert len(lexicon.functions) == 5


def test_failed_group_preserves_verified_functions_and_retries_only_missing(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path)
    path = tmp_path / "service.py"
    path.write_text(
        "".join(
            f"def function_{number}() -> int:\n    return {number}\n\n"
            for number in range(5)
        ),
        encoding="utf-8",
    )
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    code_map = load_file_code_map(tmp_path, "service.py", manifest=report.manifest)
    graph = load_relationship_graph(tmp_path, manifest=report.manifest)
    source = path.read_text(encoding="utf-8")
    maps = {"service.py": code_map}
    sources = {"service.py": source}

    def incomplete(request, call):
        if (
            request.purpose == "semantic-lexicon-verification"
            and len(request.trusted_code_map_facts["target_functions"]) == 1
        ):
            return json.dumps({"schema_version": 1, "accepted_ids": []})
        return _response(request)

    first = asyncio.run(
        analyze_file_lexicon_resumable(
            _provider(incomplete), code_map, source, graph, maps, sources
        )
    )
    assert len(first.functions) == 4
    assert len(first.missing_symbol_ids) == 1
    assert first.failure_codes == ("ValueError",)
    requests = []

    def complete(request, call):
        requests.append(request)
        return _response(request)

    second = asyncio.run(
        analyze_file_lexicon_resumable(
            _provider(complete),
            code_map,
            source,
            graph,
            maps,
            sources,
            previous=first,
        )
    )
    assert len(second.functions) == 5
    assert second.missing_symbol_ids == ()
    builds = [item for item in requests if item.purpose == "semantic-lexicon"]
    assert len(builds) == 1
    assert len(builds[0].trusted_code_map_facts["target_functions"]) == 1


def test_resumable_lexicon_reports_budget_before_any_model_call(tmp_path: Path) -> None:
    maps, graph, sources = _fixture(tmp_path)
    provider = _provider(lambda request, call: _response(request))
    result = asyncio.run(
        analyze_file_lexicon_resumable(
            provider,
            maps["service.py"],
            sources["service.py"],
            graph,
            maps,
            sources,
            request_budget=0,
        )
    )
    assert result.functions == ()
    assert result.missing_symbol_ids
    assert result.failure_codes == ("SemanticLexiconBudgetExceeded",)
    assert provider.call_count == 0


def test_resumable_lexicon_stops_after_provider_wide_failure(tmp_path: Path) -> None:
    maps, graph, sources = _fixture(tmp_path)

    def quota(request, call):
        raise ProviderQuotaError("subscription limit")

    provider = _provider(quota)
    result = asyncio.run(
        analyze_file_lexicon_resumable(
            provider,
            maps["service.py"],
            sources["service.py"],
            graph,
            maps,
            sources,
        )
    )
    assert result.functions == ()
    assert result.missing_symbol_ids
    assert "ProviderQuotaError" in result.failure_codes
    assert provider.call_count == 1


def test_resumable_lexicon_ignores_previous_different_source_sha(
    tmp_path: Path,
) -> None:
    maps, graph, sources = _fixture(tmp_path)
    first = asyncio.run(
        analyze_file_lexicon_resumable(
            _provider(lambda request, call: _response(request)),
            maps["service.py"],
            sources["service.py"],
            graph,
            maps,
            sources,
        )
    )
    stale = first.model_copy(update={"source_sha256": "0" * 64})
    provider = _provider(lambda request, call: _response(request))
    renewed = asyncio.run(
        analyze_file_lexicon_resumable(
            provider,
            maps["service.py"],
            sources["service.py"],
            graph,
            maps,
            sources,
            previous=stale,
        )
    )
    assert renewed.functions == first.functions
    assert provider.call_count > 0


def test_invented_call_edge_id_is_rejected(tmp_path: Path) -> None:
    maps, graph, sources = _fixture(tmp_path)

    def responder(request, call):
        payload = json.loads(_response(request))
        if request.purpose == "semantic-lexicon":
            payload["calls"] = [{"edge_id": "invented", "expressions": ["wrong"]}]
        return json.dumps(payload)

    with pytest.raises(ValueError, match="invented a call edge"):
        asyncio.run(
            analyze_file_lexicon(
                _provider(responder),
                maps["app.py"],
                sources["app.py"],
                graph,
                maps,
                sources,
            )
        )


def test_duplicate_call_edge_id_is_rejected(tmp_path: Path) -> None:
    maps, graph, sources = _fixture(tmp_path)

    def responder(request, call):
        payload = json.loads(_response(request))
        if request.purpose == "semantic-lexicon" and payload["calls"]:
            payload["calls"].append(payload["calls"][0])
        return json.dumps(payload)

    with pytest.raises(ValueError, match="repeated a call edge"):
        asyncio.run(
            analyze_file_lexicon(
                _provider(responder),
                maps["app.py"],
                sources["app.py"],
                graph,
                maps,
                sources,
            )
        )


def test_verifier_drops_unsupported_expressions_and_rejects_summary(
    tmp_path: Path,
) -> None:
    maps, graph, sources = _fixture(tmp_path)

    def responder(request, call):
        if request.purpose == "semantic-lexicon-verification":
            return json.dumps(
                {
                    "schema_version": 1,
                    "accepted_ids": [
                        claim_id
                        for claim_id in request.trusted_code_map_facts[
                            "proposed_claims"
                        ]
                        if claim_id.startswith("summary:")
                    ],
                }
            )
        return _response(request)

    lexicon = asyncio.run(
        analyze_file_lexicon(
            _provider(responder),
            maps["app.py"],
            sources["app.py"],
            graph,
            maps,
            sources,
        )
    )
    assert lexicon.functions[0].expressions == ()
    assert all(call.expressions == () for call in lexicon.calls)
    assert lexicon.dropped_claims >= 1

    def reject_summary(request, call):
        if request.purpose == "semantic-lexicon-verification":
            return json.dumps({"schema_version": 1, "accepted_ids": []})
        return _response(request)

    with pytest.raises(ValueError, match="summary lacks semantic support"):
        asyncio.run(
            analyze_file_lexicon(
                _provider(reject_summary),
                maps["app.py"],
                sources["app.py"],
                graph,
                maps,
                sources,
            )
        )


def test_second_server_limit_becomes_explicit_full_file_overflow(
    tmp_path: Path,
) -> None:
    maps, graph, sources = _fixture(tmp_path)

    def responder(request, call):
        return ContextWindowExceededError(
            server_context_window=8192 if call == 0 else 4096
        )

    provider = _provider(responder)
    with pytest.raises(SemanticContextOverflow, match="4096") as error:
        asyncio.run(
            analyze_file_lexicon(
                provider, maps["app.py"], sources["app.py"], graph, maps, sources
            )
        )
    assert error.value.effective_window == 4096
    assert provider.call_count == 2


def test_integrated_full_file_overflow_is_partial_and_retryable(tmp_path: Path) -> None:
    _fixture(tmp_path)
    provider = _provider(lambda request, call: _response(request), context_window=1024)
    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
            semantic_scope="all",
        )
    )
    assert report.semantic is not None
    assert "app.py" in report.semantic.failed_paths
    assert report.manifest.generation_kind == "enriched"
    from contextforge.intelligence import load_semantic_card

    card = load_semantic_card(tmp_path, "app.py", manifest=report.manifest)
    assert card.quality == "partial"
    assert any(
        item.code == "semantic_lexicon_full_file_overflow" for item in card.diagnostics
    )


def test_integrated_file_only_records_server_window(tmp_path: Path) -> None:
    _fixture(tmp_path)
    limited: set[str] = set()

    def responder(request, call):
        if request.purpose.startswith("semantic-card"):
            return json.dumps(
                {
                    "schema_version": 1,
                    "synopsis": {
                        "text": "Provides greeting behavior",
                        "evidence_ids": ["file"],
                    },
                    "concepts": [{"text": "greeting", "evidence_ids": ["symbol:0000"]}],
                    "responsibilities": [],
                    "key_symbols": [],
                    "side_effects": [],
                    "profile_facts": {},
                }
            )
        path = request.trusted_code_map_facts["path"]
        if (
            request.purpose == "semantic-lexicon"
            and path == "app.py"
            and path not in limited
        ):
            limited.add(path)
            return ContextWindowExceededError(server_context_window=8192)
        return _response(request)

    provider = _provider(responder)
    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=provider,
            provider_configuration=provider.configuration,
            semantic_scope="all",
        )
    )
    from contextforge.intelligence import load_semantic_card

    card = load_semantic_card(tmp_path, "app.py", manifest=report.manifest)
    assert card.lexicon is not None and card.lexicon.context_mode == "file_only"
    assert card.lexicon.reported_context_window == 8192
    assert any(
        item.code == "semantic_lexicon_file_only" and "8192" in item.message
        for item in card.diagnostics
    )


def test_missing_or_inconsistent_callee_source_preserves_call_ids(
    tmp_path: Path,
) -> None:
    maps, graph, sources = _fixture(tmp_path)
    requests = []

    def responder(request, call):
        requests.append(request)
        return _response(request)

    provider = _provider(responder)
    asyncio.run(
        analyze_file_lexicon(
            provider,
            maps["app.py"],
            sources["app.py"],
            graph,
            maps,
            {"app.py": sources["app.py"]},
        )
    )
    assert requests[0].trusted_code_map_facts["outgoing_calls"]
    assert [item.path for item in requests[0].untrusted_sources] == ["app.py"]

    target_ids = {edge.target_node_id for edge in graph.edges if edge.kind == "call"}
    changed = graph.model_copy(
        update={
            "nodes": tuple(
                node.model_copy(update={"path": "wrong.py"})
                if node.node_id in target_ids and node.path == "service.py"
                else node
                for node in graph.nodes
            )
        }
    )
    requests.clear()
    asyncio.run(
        analyze_file_lexicon(
            _provider(responder),
            maps["app.py"],
            sources["app.py"],
            changed,
            maps,
            {**sources, "wrong.py": sources["service.py"]},
        )
    )
    assert requests[0].trusted_code_map_facts["outgoing_calls"]
    assert [item.path for item in requests[0].untrusted_sources] == ["app.py"]


def test_preflight_file_only_server_rejection_is_explicit(tmp_path: Path) -> None:
    maps, graph, sources = _fixture(tmp_path)
    symbols = lexicon_module.callable_symbols(maps["app.py"])
    full = lexicon_module._request(
        maps["app.py"],
        sources["app.py"],
        symbols,
        graph,
        maps,
        sources,
        include_callees=True,
    )
    file_only = lexicon_module._request(
        maps["app.py"],
        sources["app.py"],
        symbols,
        graph,
        maps,
        sources,
        include_callees=False,
    )
    baseline = _provider(lambda request, call: _response(request))
    file_cost = estimate_request_context(
        file_only, baseline.configuration
    ).estimated_total_tokens
    window = next(
        size
        for size in range(max(1024, file_cost - 512), file_cost + 1024)
        if estimate_request_context(
            file_only,
            _provider(
                lambda request, call: _response(request), context_window=size
            ).configuration,
        ).fits
        and not estimate_request_context(
            full,
            _provider(
                lambda request, call: _response(request), context_window=size
            ).configuration,
        ).fits
    )
    observed = []

    def reject(request, call):
        observed.append(request)
        return ContextWindowExceededError(server_context_window=window - 1)

    provider = _provider(reject, context_window=window)
    with pytest.raises(SemanticContextOverflow) as error:
        asyncio.run(
            analyze_file_lexicon(
                provider, maps["app.py"], sources["app.py"], graph, maps, sources
            )
        )
    assert error.value.effective_window == window - 1
    assert provider.call_count == 1
    assert observed[0].trusted_code_map_facts["context_mode"] == "file_only"


@pytest.mark.parametrize(
    "wrong_purpose", ["semantic-lexicon", "semantic-lexicon-verification"]
)
def test_provider_shape_mismatch_is_rejected(
    tmp_path: Path, wrong_purpose: str
) -> None:
    maps, graph, sources = _fixture(tmp_path)
    provider = _provider(lambda request, call: _response(request))

    class WrongShapeProvider:
        configuration = provider.configuration

        async def complete_structured(self, request):
            response = await provider.complete_structured(request)
            return (
                SimpleNamespace(value=object())
                if request.purpose == wrong_purpose
                else response
            )

    with pytest.raises(ValueError, match="invalid shape"):
        asyncio.run(
            analyze_file_lexicon(
                WrongShapeProvider(),
                maps["app.py"],
                sources["app.py"],
                graph,
                maps,
                sources,
            )
        )
