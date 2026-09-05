import asyncio
import json
from pathlib import Path

import pytest

from contextforge.discovery import (
    DiscoveryBudget,
    DiscoveryKnowledge,
    DiscoveryMode,
    DiscoveryRequest,
    DiscoverySession,
    DiscoveryToolExecutor,
    ToolBudgetTracker,
    discover_repository,
)
from contextforge.discovery.dependencies import symbol_dependencies
from contextforge.discovery.session import (
    _detect_intent_facets,
    _exact_identifier_terms,
    _identifier_warnings,
    _rank_candidate_records,
    _rank_candidates_by_facet,
    _ranking_tokens,
)
from contextforge.intelligence import extract_code_maps
from contextforge.models import FakeModelProvider, ModelRequest, ProviderConfiguration
from contextforge.repositories import scan_repository


def knowledge(root: Path, files: dict[str, str]) -> DiscoveryKnowledge:
    for name, source in files.items():
        (root / name).write_text(source, encoding="utf-8")
    snapshot = scan_repository(root)
    return DiscoveryKnowledge(
        snapshot=snapshot,
        mode=DiscoveryMode.FRESH,
        code_maps={m.path: m for m in extract_code_maps(snapshot)},
    )


def test_verified_definition_beats_unlimited_comment_score(tmp_path: Path) -> None:
    data = knowledge(
        tmp_path,
        {
            "comments.ts": "// function actualWork()\n" * 100,
            "work.ts": "function actualWork() { return 1; }\n",
        },
    )
    records = _rank_candidate_records(
        data, task="Explain actualWork", pinned_paths=(), excluded_paths=()
    )
    assert records[0].path == "work.ts"
    assert records[1].score > records[0].score
    assert not any(
        s.startswith("exact_source_declarations=") for s in records[1].ranking_signals
    )
    facets = _detect_intent_facets("Explain actualWork")
    rankings = _rank_candidates_by_facet(data, records, facets)
    assert all(paths[0] == "work.ts" for paths in rankings.values() if paths)


def test_unicode_and_absent_identifier_evidence(tmp_path: Path) -> None:
    data = knowledge(tmp_path, {"work.ts": "function вычислить() { return 1; }\n"})
    assert _exact_identifier_terms("Объясни вычислить") == {"вычислить"}
    assert "вычислить" in _ranking_tokens("Объясни вычислить")
    assert _identifier_warnings(data, "Объясни вычислить") == ()
    warnings = _identifier_warnings(data, "Объясни вычислитьV2")
    assert {w.code for w in warnings} == {
        "exact-identifier-not-found",
        "low-relevance-candidates",
    }


def test_qualified_unicode_identifiers_are_kept_whole() -> None:
    assert _exact_identifier_terms("Где вызывается Модуль.вычислить?") == {
        "Модуль.вычислить"
    }
    assert _exact_identifier_terms("Find '$api.вычислитьValue' callers") == {
        "$api.вычислитьValue"
    }
    assert _exact_identifier_terms("Объясни как работает этот проект") == set()


def test_missing_identifier_request_trims_optional_inventory(tmp_path: Path) -> None:
    from contextforge.models import estimate_request_context

    for index in range(80):
        (tmp_path / (f"module_{index:03d}_" + "x" * 96 + ".ts")).write_text(
            "function preparationProgressStage(x: number) { return x; }\n",
            encoding="utf-8",
        )
    requests: list[ModelRequest] = []

    def respond(request: ModelRequest, _: int) -> str:
        requests.append(request)
        assert estimate_request_context(request, provider.configuration).fits
        candidate = request.trusted_code_map_facts["candidates"][0]["candidate_id"]
        return json.dumps(
            {
                "schema_version": 1,
                "actions": [
                    {
                        "action_id": "select",
                        "kind": "call_tool",
                        "tool_name": "select_candidates",
                        "arguments": {"candidate_ids": [candidate]},
                    },
                    {
                        "action_id": "finish",
                        "kind": "finalize",
                        "arguments": {"summary": "Approximate implementation."},
                    },
                ],
            }
        )

    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="fake://offline",
            model_id="review",
            context_window=8192,
        ),
        responder=respond,
    )
    result = asyncio.run(
        discover_repository(
            scan_repository(tmp_path),
            provider,
            DiscoveryRequest(
                task="Explain preparationProgressStageV2", mode=DiscoveryMode.FRESH
            ),
        )
    )
    assert requests[0].trusted_code_map_facts["inventories_truncated"]
    assert result.status == "complete"
    assert result.final_selection is not None
    assert result.final_selection.confidence <= 0.35
    assert any(w.code == "exact-identifier-not-found" for w in result.warnings)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "const PHASES = new Set(['index']); "
            "function run(x: string) { return PHASES.has(x); }",
            {"PHASES"},
        ),
        ("const PHASES = 1; function run(PHASES: number) { return PHASES; }", set()),
        (
            "const PHASES = 1; function run() { const PHASES = 2; return PHASES; }",
            set(),
        ),
        (
            "function other() { return run(); } function run() { return other(); }",
            {"other"},
        ),
    ],
)
def test_ast_dependencies_and_shadowing(
    tmp_path: Path, source: str, expected: set[str]
) -> None:
    data = knowledge(tmp_path, {"work.ts": source})
    code_map = data.code_maps["work.ts"]
    symbol = next(s for s in code_map.symbols if s.name == "run")
    result = symbol_dependencies(source, code_map, symbol)
    assert result.supported
    assert {
        s.name for s in code_map.symbols if s.symbol_id in result.symbol_ids
    } == expected
    assert not result.unresolved_names


@pytest.mark.parametrize(
    ("filename", "source", "builtin_name"),
    [
        (
            "work.py",
            "def len(value): return 42\ndef run(value): return len(value)\n",
            "len",
        ),
        (
            "work.ts",
            "export const Set = { has(value: string) { return true; } }; "
            "export function run(value: string) { return Set.has(value); }",
            "Set",
        ),
    ],
)
def test_shadowed_builtins_resolve_before_builtin_filtering(
    tmp_path: Path, filename: str, source: str, builtin_name: str
) -> None:
    data = knowledge(tmp_path, {filename: source})
    code_map = data.code_maps[filename]
    by_name = {symbol.name: symbol for symbol in code_map.symbols}

    result = symbol_dependencies(source, code_map, by_name["run"])

    assert by_name[builtin_name].symbol_id in result.symbol_ids
    assert not result.unresolved_names


@pytest.mark.parametrize(
    ("filename", "source"),
    [
        ("work.py", "def run(values): return len(values)\n"),
        ("work.ts", "function run(values: string[]) { return Set.from(values); }"),
    ],
)
def test_unshadowed_builtins_are_not_unresolved(
    tmp_path: Path, filename: str, source: str
) -> None:
    data = knowledge(tmp_path, {filename: source})
    code_map = data.code_maps[filename]
    symbol = next(item for item in code_map.symbols if item.name == "run")

    result = symbol_dependencies(source, code_map, symbol)

    assert not result.symbol_ids
    assert not result.unresolved_names


@pytest.mark.parametrize(
    ("files", "target", "expected"),
    [
        ({"work.py": "def target(): return 1\n"}, "work.py", "supported"),
        ({"work.ts": "function target() { return 1; }"}, "work.ts", "unsupported"),
        (
            {
                "work.ts": "function target() { return 1; }",
                "other.py": "def run(): return 1\n",
            },
            "work.ts",
            "partial",
        ),
        (
            {
                "work.py": "def target(): return 1\n",
                "Caller.kt": "fun run() = target()\n",
            },
            "work.py",
            "partial",
        ),
    ],
)
def test_empty_callers_disclose_coverage(
    tmp_path: Path, files: dict[str, str], target: str, expected: str
) -> None:
    data = knowledge(tmp_path, files)
    executor = DiscoveryToolExecutor(data, ToolBudgetTracker(DiscoveryBudget()))
    symbol = next(s for s in data.code_maps[target].symbols if s.name == "target")
    result = executor.execute(
        step=1,
        action_id="callers",
        tool_name="find_callers",
        arguments={"symbol_id": symbol.symbol_id},
    )
    assert result.ok
    assert result.data["total_matches"] == 0
    assert result.data["coverage"]["status"] == expected
    assert result.data["coverage"]["limitations"]


def test_model_can_select_symbols_and_repair_missing_dependency(tmp_path: Path) -> None:
    data = knowledge(
        tmp_path,
        {
            "progress.ts": (
                "function preparationProgressStage(x: string) "
                "{ return INDEX_PHASES.has(x); }\n"
                + "\n" * 30
                + "const INDEX_PHASES = new Set(['scan', 'index']);\n"
            )
        },
    )
    calls: list[ModelRequest] = []

    def respond(request: ModelRequest, index: int) -> str:
        calls.append(request)
        facts = request.trusted_code_map_facts
        ids = [facts["candidates"][0]["candidate_id"]]
        symbols = facts["symbols"]
        assert {s["name"] for s in symbols} == {
            "preparationProgressStage",
            "INDEX_PHASES",
        }
        selected = [
            s["symbol_id"]
            for s in symbols
            if index > 0 or s["name"] == "preparationProgressStage"
        ]
        if request.response_model.__name__ == "IndexedContextSelection":
            return json.dumps(
                {
                    "schema_version": 1,
                    "candidate_ids": ids,
                    "symbol_ids": selected,
                    "summary": "Selected functions.",
                }
            )
        return json.dumps(
            {
                "schema_version": 1,
                "actions": [
                    {
                        "action_id": "select",
                        "kind": "call_tool",
                        "tool_name": "select_candidates",
                        "arguments": {"candidate_ids": ids, "symbol_ids": selected},
                    },
                    {
                        "action_id": "finish",
                        "kind": "finalize",
                        "arguments": {"summary": "Function and phases."},
                    },
                ],
            }
        )

    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="fake://offline",
            model_id="review",
            context_window=32768,
        ),
        responder=respond,
    )
    result = asyncio.run(
        discover_repository(
            data.snapshot,
            provider,
            DiscoveryRequest(
                task="Объясни preparationProgressStage", mode=DiscoveryMode.FRESH
            ),
        )
    )
    assert result.final_selection is not None
    assert len(calls) == 2
    assert any(
        r.start_line <= 32 <= r.end_line
        for r in result.final_selection.selected[0].ranges
    )
    assert not any(
        w.code == "symbol-dependencies-omitted"
        for w in result.final_selection.completeness_warnings
    )


def test_symbol_selection_rejects_unknown_id_without_mutation(tmp_path: Path) -> None:
    data = knowledge(tmp_path, {"work.ts": "function run() {}\n"})
    session = DiscoverySession(
        data.snapshot, None, DiscoveryRequest(task="Explain run")
    )
    executor, _ = session.prepare_read_only_tools()
    result = executor.execute(
        step=1,
        action_id="invalid",
        tool_name="select_candidates",
        arguments={
            "candidate_ids": [session._preselected_candidates[0].candidate_id],
            "symbol_ids": ["missing"],
        },
    )
    assert not result.ok
    assert not executor.selected


def test_symbol_selection_keeps_functions_selected_in_previous_steps(
    tmp_path: Path,
) -> None:
    data = knowledge(
        tmp_path,
        {"work.ts": "function first() {}\n\n\nfunction second() {}\n"},
    )
    session = DiscoverySession(data.snapshot, None, DiscoveryRequest(task="work"))
    executor, _ = session.prepare_read_only_tools()
    candidate_id = session._preselected_candidates[0].candidate_id
    for symbol in data.code_maps["work.ts"].symbols:
        result = executor.execute(
            step=1,
            action_id=symbol.name,
            tool_name="select_candidates",
            arguments={
                "candidate_ids": [candidate_id],
                "symbol_ids": [symbol.symbol_id],
            },
        )
        assert result.ok
    assert [(r.start_line, r.end_line) for r in executor.selected[0].ranges] == [
        (1, 1),
        (4, 4),
    ]


@pytest.mark.parametrize(
    ("source", "name", "expected"),
    [
        ("const LIMIT = 2; function run(x = LIMIT) { return x; }", "run", {"LIMIT"}),
        (
            "const LIMIT = 2; function run({x = LIMIT}: {x?: number} = {}) "
            "{ return x; }",
            "run",
            {"LIMIT"},
        ),
        (
            "const LIMIT = 2; function run([x = LIMIT]: number[] = []) { return x; }",
            "run",
            {"LIMIT"},
        ),
        (
            "const LIMIT = 2; function run() { "
            "if (true) { let LIMIT = 1; } return LIMIT; }",
            "run",
            {"LIMIT"},
        ),
        (
            "const LIMIT = 2; function run() { "
            "if (true) { var LIMIT = 1; } return LIMIT; }",
            "run",
            set(),
        ),
        (
            "const LIMIT = 2; function run() { "
            "for (var LIMIT = 0; LIMIT < 1; LIMIT++) {} return LIMIT; }",
            "run",
            set(),
        ),
        (
            "const LIMIT = 2; function run() { "
            "function inner() { var LIMIT = 1; return LIMIT; } "
            "return LIMIT + inner(); }",
            "run",
            {"LIMIT"},
        ),
        ("const fn = x => x + 1;", "fn", set()),
        ("const fn = ({value}) => value;", "fn", set()),
        ("class A { other() {} run() { this.other(); } }", "run", {"other"}),
        ("function run() { return missing(); }", "run", set()),
    ],
)
def test_dependency_scopes_and_unresolved_references(
    tmp_path: Path, source: str, name: str, expected: set[str]
) -> None:
    data = knowledge(tmp_path, {"work.ts": source})
    code_map = data.code_maps["work.ts"]
    symbol = next(s for s in code_map.symbols if s.name == name)
    result = symbol_dependencies(source, code_map, symbol)
    assert {
        s.name for s in code_map.symbols if s.symbol_id in result.symbol_ids
    } == expected
    if "missing()" in source:
        assert result.unresolved_names == ("missing",)
    else:
        assert not result.unresolved_names


def test_python_decorated_function_and_constant_dependencies(tmp_path: Path) -> None:
    source = (
        "LIMIT = 1\nALIAS = LIMIT\ndef deco(f): return f\n"
        "@deco\ndef run(x): return ALIAS + x\n"
    )
    data = knowledge(tmp_path, {"work.py": source})
    code_map = data.code_maps["work.py"]
    by_name = {s.name: s for s in code_map.symbols}
    deps = symbol_dependencies(source, code_map, by_name["run"])
    assert by_name["ALIAS"].symbol_id in deps.symbol_ids
    assert by_name["deco"].symbol_id in deps.symbol_ids
    assert symbol_dependencies(source, code_map, by_name["ALIAS"]).symbol_ids == (
        by_name["LIMIT"].symbol_id,
    )
    assert not symbol_dependencies("def broken(", code_map, by_name["run"]).supported


@pytest.mark.parametrize(
    ("body", "needs_limit"),
    [
        ("def inner(LIMIT): return LIMIT\n return LIMIT", True),
        ("LIMIT = 2\n def inner(): return LIMIT\n return inner()", False),
        ("return [LIMIT for LIMIT in range(3)]", False),
        ("return [x for x in LIMIT]", True),
    ],
)
def test_python_nested_scopes_preserve_external_references(
    tmp_path: Path, body: str, needs_limit: bool
) -> None:
    source = "LIMIT = [1, 2]\ndef run():\n " + body + "\n"
    data = knowledge(tmp_path, {"work.py": source})
    code_map = data.code_maps["work.py"]
    by_name = {s.name: s for s in code_map.symbols}
    result = symbol_dependencies(source, code_map, by_name["run"])
    assert result.supported
    assert (by_name["LIMIT"].symbol_id in result.symbol_ids) == needs_limit
    assert not result.unresolved_names


def test_limited_exact_search_is_not_reported_as_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextforge.discovery.session as module

    data = knowledge(tmp_path, {"work.ts": "function present() {}"})
    monkeypatch.setattr(module, "_MAX_EXACT_SCAN_BYTES", 0)
    warnings = _identifier_warnings(data, "Explain missingSymbol")
    assert "exact-identifier-search-limited" in {w.code for w in warnings}
    assert "exact-identifier-not-found" not in {w.code for w in warnings}


def test_old_analyzer_and_unsupported_dependency_coverage(tmp_path: Path) -> None:
    from contextforge.intelligence.coverage import relationship_coverage

    data = knowledge(tmp_path, {"work.rs": "fn run() {}"})
    code_map = data.code_maps["work.rs"]
    assert not symbol_dependencies(
        "fn run() {}", code_map, code_map.symbols[0]
    ).supported
    old = code_map.model_copy(
        update={
            "analyzer": code_map.analyzer.model_copy(update={"analyzer_version": "old"})
        }
    )
    assert relationship_coverage({"work.rs": old}, ("work.rs",))["status"] == "unknown"


def test_unsupported_source_languages_are_counted_in_repository_coverage(
    tmp_path: Path,
) -> None:
    from contextforge.intelligence.coverage import (
        relationship_coverage,
        relationship_source_paths,
    )

    data = knowledge(
        tmp_path,
        {
            "work.py": "def target(): return 1\n",
            "Caller.kt": "fun run() = target()\n",
            "README.md": "target is documented here\n",
        },
    )

    coverage = relationship_coverage(
        data.code_maps, relationship_source_paths(data.snapshot.files)
    )

    assert coverage["status"] == "partial"
    assert coverage["file_counts"] == {
        "supported": 1,
        "partial": 0,
        "unsupported": 1,
        "unknown": 0,
    }
