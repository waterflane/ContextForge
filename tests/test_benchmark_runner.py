import asyncio
import json
from pathlib import Path
from typing import Any

from contextforge.benchmarks import (
    BenchmarkExpectations,
    BenchmarkManifest,
    BenchmarkMode,
    BenchmarkModeOverrides,
    BenchmarkTask,
    run_discovery_benchmark,
)
from contextforge.models import FakeModelProvider, ModelRequest, ProviderConfiguration


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
    assert result.passed is False
