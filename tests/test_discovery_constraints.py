from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from contextforge.application import build_discovery_request
from contextforge.benchmarks import (
    BenchmarkManifest,
    BenchmarkMode,
    BenchmarkTask,
    run_discovery_benchmark,
)
from contextforge.discovery import (
    DiscoveryBudget,
    DiscoveryKnowledge,
    DiscoveryMode,
    DiscoveryRequest,
    DiscoveryRunRecord,
    discover_repository,
)
from contextforge.discovery.constraints import extract_task_file_constraints
from contextforge.discovery.session import (
    _detect_intent_facets,
    _facet_aware_preselection,
    _rank_candidate_records,
    _rank_candidates_by_facet,
    _ranking_tokens,
)
from contextforge.models import FakeModelProvider, ProviderConfiguration
from contextforge.repositories import scan_repository


def _configuration() -> ProviderConfiguration:
    return ProviderConfiguration(
        provider_id="fake",
        endpoint="fake://offline",
        model_id="constraint-fallback",
        retry_limit=0,
        max_json_repair_attempts=5,
        context_window=98_304,
    )


def _fallback(
    root: Path,
    *,
    task: str,
    max_files: int = 100,
    pinned_paths: tuple[str, ...] = (),
) -> DiscoveryRunRecord:
    return asyncio.run(
        discover_repository(
            scan_repository(root),
            FakeModelProvider(_configuration(), scripts=['{"schema_version":1}'] * 3),
            DiscoveryRequest(
                task=task,
                mode=DiscoveryMode.FRESH,
                pinned_paths=pinned_paths,
                budget=DiscoveryBudget(max_context_files=max_files),
            ),
        )
    )


def _selected(result: DiscoveryRunRecord) -> tuple[str, ...]:
    selection = result.final_selection
    assert selection is not None
    return tuple(item.path for item in selection.selected if item.path is not None)


def _write_files(root: Path, paths: tuple[str, ...]) -> None:
    for index, relative in enumerate(paths):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"VALUE_{index} = {index}\n", encoding="utf-8")


def test_simple_negative_setup_instruction_excludes_matching_helpers(
    tmp_path: Path,
) -> None:
    _write_files(
        tmp_path,
        ("app.py", "setup-global-access.bat", "setup-global-access.ps1"),
    )

    result = _fallback(
        tmp_path,
        task="Find global access; do not select setup helpers.",
    )

    assert _selected(result) == ("app.py",)


def test_all_negative_task_does_not_reintroduce_negated_relevance() -> None:
    constraints = extract_task_file_constraints("Do not select setup helpers.")

    assert constraints.positive_task == ""
    assert constraints.excludes("setup-global-access.ps1")


def test_negative_context_spend_list_excludes_each_named_file_role() -> None:
    constraints = extract_task_file_constraints(
        "Locate media search. Do not spend context on tests, documentation, "
        "setup helpers, or integration callers."
    )

    assert constraints.positive_task == "Locate media search"
    assert constraints.excludes("test/media-search.test.js")
    assert constraints.excludes("scripts/setup-access.ps1")
    assert not constraints.excludes("media-search.js")


def test_negative_expand_clause_excludes_unrequested_application_roles() -> None:
    constraints = extract_task_file_constraints(
        "Find the browser bootstrap. Do not expand into server startup or the "
        "separate video-library page."
    )

    assert constraints.positive_task == "Find the browser bootstrap"
    assert constraints.excludes("server.js")
    assert constraints.excludes("public/videos.html")
    assert not constraints.excludes("public/app.js")


@pytest.mark.parametrize("verb", ["do not include", "exclude", "ignore", "without"])
def test_explicit_filenames_in_negative_clauses_are_excluded(
    tmp_path: Path, verb: str
) -> None:
    _write_files(tmp_path, ("app.py", "setup-global-access.ps1"))

    result = _fallback(
        tmp_path,
        task=f"Find app behavior; {verb} setup-global-access.ps1.",
    )

    assert _selected(result) == ("app.py",)


def test_negative_path_pattern_is_excluded_and_removed_from_positive_task() -> None:
    constraints = extract_task_file_constraints(
        "Exclude scripts/setup-*.ps1; inspect app.py."
    )

    assert constraints.positive_task == "inspect app.py"
    assert constraints.excludes("scripts/setup-global-access.ps1")
    assert not constraints.excludes("setup-global-access.ps1")
    assert constraints.excluded_terms == frozenset()


def test_positive_setup_request_still_selects_setup_files(tmp_path: Path) -> None:
    _write_files(
        tmp_path,
        ("app.py", "setup-global-access.bat", "setup-global-access.ps1"),
    )

    result = _fallback(tmp_path, task="Select the setup global access files.")

    assert {
        "setup-global-access.bat",
        "setup-global-access.ps1",
    } <= set(_selected(result))


def test_unrelated_without_does_not_create_file_exclusions(tmp_path: Path) -> None:
    _write_files(tmp_path, ("app.py", "changing.py", "setup-runtime.py"))

    constraints = extract_task_file_constraints(
        "Find setup behavior without changing files or runtime behavior"
    )
    result = _fallback(
        tmp_path,
        task="Find setup behavior without changing files or runtime behavior",
    )

    assert constraints.excluded_references == ()
    assert constraints.excluded_terms == frozenset()
    assert "setup-runtime.py" in _selected(result)


@pytest.mark.parametrize(("limit", "expected"), [(1, 1), (3, 1)])
def test_explicit_hard_file_limit_caps_minimal_fallback(
    tmp_path: Path, limit: int, expected: int
) -> None:
    _write_files(
        tmp_path,
        tuple(f"candidate_{index}.py" for index in range(5)),
    )

    result = _fallback(tmp_path, task="Select candidate files", max_files=limit)

    assert len(_selected(result)) == expected


def test_pinned_required_file_is_preserved_with_hard_limit(tmp_path: Path) -> None:
    _write_files(tmp_path, ("candidate.py", "required.py"))

    result = _fallback(
        tmp_path,
        task="Select candidate files",
        max_files=1,
        pinned_paths=("required.py",),
    )

    assert _selected(result) == ("required.py",)
    assert result.final_selection is not None
    assert result.final_selection.selected[0].manually_pinned


def test_mandatory_pins_exceeding_limit_are_preserved_with_diagnostic(
    tmp_path: Path,
) -> None:
    _write_files(tmp_path, ("candidate.py", "required_a.py", "required_b.py"))

    request = build_discovery_request(
        task="Select candidate files",
        mode="fresh",
        includes=("required_a.py", "required_b.py"),
        max_files=1,
    )
    result = _fallback(
        tmp_path,
        task="Select candidate files",
        max_files=1,
        pinned_paths=("required_a.py", "required_b.py"),
    )

    assert request.budget.max_context_files == 1
    assert request.pinned_paths == ("required_a.py", "required_b.py")
    assert set(_selected(result)) == {"required_a.py", "required_b.py"}
    assert {item.code for item in result.warnings} >= {
        "mandatory-pins-exceed-file-limit"
    }
    assert all(
        item.manually_pinned
        for item in result.final_selection.selected  # type: ignore[union-attr]
    )


def test_benchmark_selected_file_budget_is_evaluation_only(tmp_path: Path) -> None:
    repository = tmp_path / "fixture"
    repository.mkdir()
    _write_files(
        repository,
        ("setup-access.bat", "setup-access.ps1", "unrelated.py"),
    )
    task = BenchmarkTask(
        task_id="evaluation-budget-only",
        repository_path="fixture",
        task="Select the setup access files",
        modes=(BenchmarkMode.FRESH,),
        max_selected_files=1,
        max_files_read=100,
        max_model_generations=1,
    )
    manifest = BenchmarkManifest(
        schema_version=1,
        suite_name="constraint boundary",
        tasks=(task,),
    )

    benchmark = asyncio.run(
        run_discovery_benchmark(
            manifest,
            tmp_path,
            FakeModelProvider(_configuration(), scripts=['{"schema_version":1}'] * 3),
        )
    )

    run = benchmark.runs[0]
    assert set(run.selected_files) == {"setup-access.bat", "setup-access.ps1"}
    assert run.budgets.selected_files.limit == 1
    assert run.budgets.selected_files.passed is False


def test_snapshot_only_text_files_remain_rankable_without_codemap(
    tmp_path: Path,
) -> None:
    _write_files(tmp_path, ("web/bootstrap.js", "web/home.html", "settings.json"))
    snapshot = scan_repository(tmp_path)
    knowledge = DiscoveryKnowledge(
        snapshot=snapshot,
        mode=DiscoveryMode.INDEXED,
        code_maps={},
    )

    records = _rank_candidate_records(
        knowledge,
        task="Trace the browser bootstrap and configuration",
        pinned_paths=(),
        excluded_paths=(),
    )

    assert {item.path for item in records} == {
        "settings.json",
        "web/bootstrap.js",
        "web/home.html",
    }
    assert all("current_snapshot_source" in item.ranking_signals for item in records)


def test_role_facets_rank_generic_startup_files_without_fixture_names(
    tmp_path: Path,
) -> None:
    _write_files(
        tmp_path,
        (
            "package.json",
            "backend/main.js",
            "web/bootstrap.js",
            "scripts/setup-access.ps1",
        ),
    )
    snapshot = scan_repository(tmp_path)
    knowledge = DiscoveryKnowledge(
        snapshot=snapshot,
        mode=DiscoveryMode.INDEXED,
        code_maps={},
    )
    task = "Trace npm startup through the Node server to the browser bootstrap"
    records = _rank_candidate_records(
        knowledge,
        task=task,
        pinned_paths=(),
        excluded_paths=(),
    )
    facets = _detect_intent_facets(task)
    selected = _facet_aware_preselection(
        records,
        facets,
        _rank_candidates_by_facet(knowledge, records, facets),
        limit=5,
    )

    assert {
        "package.json",
        "backend/main.js",
        "web/bootstrap.js",
    } <= {item.path for item in selected}
    assert "scripts/setup-access.ps1" not in {item.path for item in selected}


def test_ranking_tokens_normalize_conservative_aliases_and_stopwords() -> None:
    assert _ranking_tokens("Configuration from videos and providers") == {
        "config",
        "provider",
        "video",
    }


def test_role_scoring_does_not_turn_index_coverage_into_test_request(
    tmp_path: Path,
) -> None:
    _write_files(
        tmp_path,
        ("server.js", "public/app.js", "test/service.test.js"),
    )
    snapshot = scan_repository(tmp_path)
    knowledge = DiscoveryKnowledge(
        snapshot=snapshot,
        mode=DiscoveryMode.INDEXED,
        code_maps={},
    )

    records = _rank_candidate_records(
        knowledge,
        task="Locate server integration and report stale-index coverage diagnostics",
        pinned_paths=(),
        excluded_paths=(),
    )
    signals = {item.path: set(item.ranking_signals) for item in records}

    assert "server_entry_role" in signals["server.js"]
    assert "server_entry_role" not in signals["public/app.js"]
    assert "requested_test_role" not in signals["test/service.test.js"]


def test_preselection_does_not_append_an_unrelated_neutral_file(tmp_path: Path) -> None:
    _write_files(tmp_path, ("media-search.js", "unrelated.txt"))
    snapshot = scan_repository(tmp_path)
    knowledge = DiscoveryKnowledge(
        snapshot=snapshot,
        mode=DiscoveryMode.INDEXED,
        code_maps={},
    )
    task = "Locate the media search service"
    records = _rank_candidate_records(
        knowledge,
        task=task,
        pinned_paths=(),
        excluded_paths=(),
    )
    facets = _detect_intent_facets(task)

    selected = _facet_aware_preselection(
        records,
        facets,
        _rank_candidates_by_facet(knowledge, records, facets),
        limit=10,
    )

    assert [item.path for item in selected] == ["media-search.js"]


def test_fallback_order_is_deterministic_across_repeated_runs(tmp_path: Path) -> None:
    _write_files(
        tmp_path,
        tuple(f"candidate_{index}.py" for index in range(5)),
    )

    first = _fallback(tmp_path, task="Select candidate files", max_files=3)
    second = _fallback(tmp_path, task="Select candidate files", max_files=3)

    assert _selected(first) == _selected(second)
