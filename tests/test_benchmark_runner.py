import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import contextforge.benchmarks.runner as benchmark_runner
from contextforge.benchmarks import (
    BenchmarkExpectations,
    BenchmarkManifest,
    BenchmarkMode,
    BenchmarkModeOverrides,
    BenchmarkTask,
    run_discovery_benchmark,
)
from contextforge.intelligence import (
    acquire_index_lock,
    build_structural_index,
    initialize_index,
)
from contextforge.models import FakeModelProvider, ModelRequest, ProviderConfiguration
from contextforge.repositories import scan_repository


def _write(root: Path, path: str, content: str) -> None:
    destination = root.joinpath(*path.split("/"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8", newline="")


def _provider(*, repair: bool = False) -> FakeModelProvider:
    configuration = ProviderConfiguration(
        provider_id="fake",
        endpoint="fake://offline",
        model_id="benchmark-v1",
        timeout_seconds=2,
        retry_limit=0,
        max_json_repair_attempts=1 if repair else 0,
    )

    selected_candidate_ids: list[str] = []

    def respond(request: ModelRequest, call_index: int) -> str:
        facts = request.trusted_code_map_facts
        candidate_ids = facts.get("allowed_candidate_ids")
        if isinstance(candidate_ids, list):
            selected_candidate_ids[:] = candidate_ids[:1]
        if repair and call_index == 0:
            return "{"
        return json.dumps(
            {
                "schema_version": 1,
                "candidate_ids": selected_candidate_ids,
                "summary": "Selected the highest-ranked candidate.",
            }
        )

    return FakeModelProvider(configuration, responder=respond)


def _fallback_provider() -> FakeModelProvider:
    configuration = ProviderConfiguration(
        provider_id="fake",
        endpoint="fake://offline",
        model_id="benchmark-v1",
        timeout_seconds=2,
        retry_limit=0,
        max_json_repair_attempts=1,
    )
    return FakeModelProvider(configuration, responder=lambda _request, _index: "{")


def _build_index(repository: Path) -> None:
    initialize_index(repository)
    with acquire_index_lock(repository, "benchmark-fixture") as lock:
        build_structural_index(scan_repository(repository), lock)


def _task(task_id: str, repository_path: str, **values: Any) -> BenchmarkTask:
    defaults: dict[str, Any] = {
        "task_id": task_id,
        "repository_path": repository_path,
        "task": "Find the main implementation.",
        "modes": (BenchmarkMode.HYBRID,),
        "include_paths": ("main.py",),
        "required_files_all": ("main.py",),
        "required_files_any": (("alternate.py", "main.py"),),
        "forbidden_files": ("secret.py",),
        "expected_facets": ("main implementation",),
        "allowed_warnings": ("hybrid-index-unavailable",),
        "max_selected_files": 1,
        "max_files_read": 3,
        "max_model_generations": 2,
        "max_provider_http_calls": 2,
    }
    defaults.update(values)
    return BenchmarkTask(**defaults)


def test_runner_records_discovery_metrics_and_evaluates_manifest(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _write(repository, "main.py", "def main():\n    return 1\n")
    _write(repository, "alternate.py", "def alternate():\n    return 2\n")
    task = _task("main", "repository")
    manifest = BenchmarkManifest(
        schema_version=1,
        suite_name="runner",
        tasks=(task,),
    )

    times = iter((10.0, 10.125))
    result = asyncio.run(
        run_discovery_benchmark(
            manifest,
            tmp_path,
            _provider(repair=True),
            clock=lambda: next(times),
        )
    )

    assert result.passed is True, result.runs[0]
    assert result.model_dump_json() == result.model_dump_json()
    run = result.runs[0]
    assert run.duration_ms == 125
    assert run.selected_files == ("main.py",)
    assert run.files_considered == 2
    assert run.files_read == 3
    assert run.context_bytes == len(b"def main():\n    return 1\n")
    assert run.provider_counters.model_generations == 1
    assert run.provider_counters.model_calls == 1
    assert run.provider_counters.repair_generations == 1
    assert run.provider_counters.auxiliary_provider_calls == 0
    assert run.provider_counters.transport_attempts == 2
    assert run.provider_counters.total_provider_http_calls == 2
    assert run.confidence is not None
    assert run.provenance == "model"
    assert run.fallback_used is False
    assert run.expectations.any_file_groups[0].matched_files == ("main.py",)
    assert run.expectations.expected_facets == ("main implementation",)
    assert run.budgets.passed is True
    assert run.failure is None
    assert not (repository / ".contextforge").exists()
    metric = result.metrics[0]
    assert metric.stability_kind == "insufficient_data"
    assert metric.exact_selected_file_match_rate is None
    assert metric.required_file_recall == 1.0
    assert metric.expected_facet_coverage_rate == 1.0
    assert metric.duration is not None
    assert metric.duration.percentiles is None


def test_runner_records_deterministic_fallback_state(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _write(repository, "main.py", "value = 1\n")
    manifest = BenchmarkManifest(
        schema_version=1,
        suite_name="fallback",
        tasks=(_task("fallback", "repository"),),
    )

    result = asyncio.run(
        run_discovery_benchmark(manifest, tmp_path, _fallback_provider())
    )

    run = result.runs[0]
    assert run.status == "complete"
    assert run.provenance == "deterministic_fallback"
    assert run.fallback_used is True
    assert run.selected_files == ("main.py",)


def test_runner_keeps_failures_and_continues_completed_tasks(tmp_path: Path) -> None:
    repository = tmp_path / "working"
    _write(repository, "main.py", "value = 1\n")
    manifest = BenchmarkManifest(
        schema_version=1,
        suite_name="continue",
        tasks=(
            _task("a-working", "working"),
            _task("b-missing", "missing"),
        ),
    )

    result = asyncio.run(run_discovery_benchmark(manifest, tmp_path, _provider()))

    assert result.passed is False
    assert tuple(run.status for run in result.runs) == ("complete", "failed")
    assert result.runs[0].selected_files == ("main.py",)
    assert result.runs[1].failure is not None
    assert result.runs[1].failure.code == "application_error"


def test_clean_index_precondition_permits_matching_indexed_task(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _write(repository, "main.py", "def main():\n    return 1\n")
    _write(repository, "alternate.py", "def alternate():\n    return 2\n")
    _build_index(repository)
    task = _task(
        "indexed",
        "repository",
        modes=(BenchmarkMode.INDEXED,),
        index_precondition={"kind": "clean"},
        allowed_warnings=(),
    )
    manifest = BenchmarkManifest(
        schema_version=1,
        suite_name="clean-index",
        tasks=(task,),
    )

    result = asyncio.run(run_discovery_benchmark(manifest, tmp_path, _provider()))

    assert result.passed is True, result.runs[0]
    assert result.runs[0].status == "complete"
    assert result.runs[0].failure is None


def test_clean_index_precondition_fails_before_discovery_and_keeps_other_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    _write(repository, "main.py", "value = 1\n")
    _write(repository, "alternate.py", "value = 2\n")
    _build_index(repository)
    _write(repository, "main.py", "value = 3\n")
    calls = 0
    original = benchmark_runner.suggest_repository_context

    async def observed(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(benchmark_runner, "suggest_repository_context", observed)
    manifest = BenchmarkManifest(
        schema_version=1,
        suite_name="precondition-continuation",
        tasks=(
            _task(
                "a-mismatched",
                "repository",
                modes=(BenchmarkMode.INDEXED,),
                index_precondition={"kind": "clean"},
            ),
            _task(
                "b-unrelated",
                "repository",
                modes=(BenchmarkMode.FRESH,),
                allowed_warnings=(),
            ),
        ),
    )

    result = asyncio.run(run_discovery_benchmark(manifest, tmp_path, _provider()))

    assert tuple(run.task_id for run in result.runs) == (
        "a-mismatched",
        "b-unrelated",
    )
    failed, unrelated = result.runs
    assert failed.status == "failed"
    assert failed.failure is not None
    assert failed.failure.code == "benchmark_precondition_failed"
    assert failed.expectations.passed is True
    assert failed.expectations.missing_required_files == ()
    assert unrelated.status == "complete"
    assert calls == 1


def test_isolated_stale_index_task_warns_and_leaves_repository_read_only(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _write(repository, "README.md", "# Fixture\n")
    _write(repository, "main.py", "def main():\n    return 1\n")
    _write(repository, "alternate.py", "def alternate():\n    return 2\n")
    _build_index(repository)
    before = {
        path.relative_to(repository): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file()
    }
    task = _task(
        "isolated-stale",
        "repository",
        modes=(BenchmarkMode.INDEXED,),
        index_precondition={
            "kind": "isolated-stale",
            "drift_path": "README.md",
        },
        allowed_warnings=(
            "incomplete-parse-data",
            "stale-global-maps",
            "stale-index-coverage",
        ),
        required_warnings=("stale-global-maps", "stale-index-coverage"),
    )
    manifest = BenchmarkManifest(
        schema_version=1,
        suite_name="isolated-stale-index",
        tasks=(task,),
    )

    result = asyncio.run(run_discovery_benchmark(manifest, tmp_path, _provider()))

    assert result.passed is True, result.runs[0]
    assert {warning.code for warning in result.runs[0].warnings} >= {
        "stale-global-maps",
        "stale-index-coverage",
    }
    assert {
        path.relative_to(repository): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file()
    } == before


def test_mode_overrides_control_repeats_and_budget_evaluation(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _write(repository, "main.py", "value = 1\n")
    task = _task(
        "overrides",
        "repository",
        mode_overrides=BenchmarkModeOverrides(
            hybrid=BenchmarkExpectations(
                repeat_count=2,
                max_model_generations=0,
            )
        ),
    )
    manifest = BenchmarkManifest(
        schema_version=1,
        suite_name="overrides",
        tasks=(task,),
    )

    result = asyncio.run(run_discovery_benchmark(manifest, tmp_path, _provider()))

    assert tuple(run.repetition for run in result.runs) == (1, 2)
    assert all(run.status == "complete" for run in result.runs)
    assert all(run.expectations.passed for run in result.runs), tuple(
        run.expectations for run in result.runs
    )
    assert all(not run.budgets.model_generations.passed for run in result.runs)
    assert result.metrics[0].stability_kind == "semantic_stability"
    assert result.metrics[0].exact_selected_file_match_rate == 1.0
    assert result.metrics[0].exact_ordered_match_rate == 1.0
    assert result.passed is False
