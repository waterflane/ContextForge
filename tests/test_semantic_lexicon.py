import asyncio
import json
from pathlib import Path

import pytest

from contextforge.application import build_repository_index
from contextforge.intelligence import load_file_code_map, load_relationship_graph
from contextforge.intelligence.semantic_lexicon import (
    SemanticContextOverflow,
    SemanticLexiconBudgetExceeded,
    analyze_file_lexicon,
)
from contextforge.models import (
    ContextWindowExceededError,
    FakeModelProvider,
    ProviderConfiguration,
)


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
