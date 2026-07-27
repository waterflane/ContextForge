"""Application-layer execution of validated discovery benchmark manifests."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from contextforge.application import build_discovery_request, suggest_repository_context
from contextforge.benchmarks.models import (
    BenchmarkAnyFileExpectation,
    BenchmarkBudgetEvaluation,
    BenchmarkExpectationEvaluation,
    BenchmarkFailure,
    BenchmarkLimitEvaluation,
    BenchmarkManifest,
    BenchmarkMode,
    BenchmarkProviderCounters,
    BenchmarkResult,
    BenchmarkRunResult,
    BenchmarkTask,
)
from contextforge.discovery import DiscoveryError, DiscoveryRunRecord
from contextforge.models import ModelProvider
from contextforge.progress import ProgressEvent


class _RunMetrics:
    """Collect non-domain metrics exposed by the normal application workflow."""

    def __init__(self) -> None:
        self.files_considered = 0

    def observe(self, event: ProgressEvent) -> None:
        value = event.metadata.get("structural_files")
        if type(value) is int:
            self.files_considered = max(self.files_considered, value)


async def run_discovery_benchmark(
    manifest: BenchmarkManifest,
    repository_root: str | Path,
    provider: ModelProvider,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> BenchmarkResult:
    """Execute every configured task and retain a canonical result for each run."""

    root = Path(repository_root).resolve()
    runs: list[BenchmarkRunResult] = []
    for task in manifest.tasks:
        for mode in task.modes:
            repeat_count = _effective(task, mode, "repeat_count")
            for repetition in range(1, repeat_count + 1):
                runs.append(
                    await _run_once(
                        root,
                        task,
                        mode,
                        repetition,
                        provider,
                        clock=clock,
                    )
                )
    return BenchmarkResult(
        manifest_schema_version=manifest.schema_version,
        suite_name=manifest.suite_name,
        runs=tuple(runs),
        passed=all(run.passed for run in runs),
    )


async def _run_once(
    root: Path,
    task: BenchmarkTask,
    mode: BenchmarkMode,
    repetition: int,
    provider: ModelProvider,
    *,
    clock: Callable[[], float],
) -> BenchmarkRunResult:
    metrics = _RunMetrics()
    started = clock()
    run_record: DiscoveryRunRecord | None = None
    failure: BenchmarkFailure | None = None
    try:
        repository = root.joinpath(*task.repository_path.split("/")).resolve()
        repository.relative_to(root)
        request = build_discovery_request(
            task=task.task,
            mode=mode.value,
            includes=_effective(task, mode, "include_paths"),
            excludes=_effective(task, mode, "exclude_paths"),
        )
        run_record = await suggest_repository_context(
            repository,
            provider,
            request,
            progress=metrics.observe,
            persist_diagnostics=False,
        )
    except DiscoveryError as exc:
        run_record = exc.run_record
        failure = _failure(
            run_record.failure_code or "discovery_failed",
            exc,
            run_record.failure_message,
        )
    except Exception as exc:
        failure = _failure("application_error", exc)
    duration_ms = max(0, round((clock() - started) * 1_000))
    return _build_result(
        task,
        mode,
        repetition,
        provider,
        run_record,
        failure,
        metrics.files_considered,
        duration_ms,
    )


def _build_result(
    task: BenchmarkTask,
    mode: BenchmarkMode,
    repetition: int,
    provider: ModelProvider,
    run: DiscoveryRunRecord | None,
    failure: BenchmarkFailure | None,
    files_considered: int,
    duration_ms: int,
) -> BenchmarkRunResult:
    selection = None if run is None else run.final_selection
    selected_files = (
        ()
        if selection is None
        else tuple(item.path for item in selection.selected if item.path is not None)
    )
    warnings = () if run is None else run.warnings
    usage = None if run is None else run.budget_usage
    counters = BenchmarkProviderCounters(
        model_generations=0 if usage is None else usage.model_generations,
        repair_generations=0 if usage is None else usage.repair_generations,
        auxiliary_provider_calls=(
            0
            if usage is None
            else usage.provider_discovery_calls + usage.provider_capability_calls
        ),
        provider_discovery_calls=(
            0 if usage is None else usage.provider_discovery_calls
        ),
        provider_capability_calls=(
            0 if usage is None else usage.provider_capability_calls
        ),
        transport_attempts=0 if usage is None else usage.transport_attempts,
        total_provider_http_calls=(
            0 if usage is None else usage.total_provider_http_calls
        ),
    )
    expectations = _evaluate_expectations(
        task, mode, selected_files, tuple(item.code for item in warnings)
    )
    budgets = _evaluate_budgets(
        task,
        mode,
        selected_files=len(selected_files),
        files_read=0 if usage is None else usage.files_read,
        counters=counters,
    )
    status = "failed" if run is None else run.status
    if run is not None and status != "complete" and failure is None:
        failure = BenchmarkFailure(
            code=(run.failure_code or "discovery_failed"),
            error_type="DiscoveryError",
            message=(run.failure_message or "Discovery did not complete."),
        )
    return BenchmarkRunResult(
        task_id=task.task_id,
        repository_path=task.repository_path,
        mode=mode,
        repetition=repetition,
        status=status,
        passed=status == "complete" and expectations.passed and budgets.passed,
        duration_ms=duration_ms,
        selected_files=selected_files,
        files_considered=max(files_considered, len(selected_files)),
        files_read=0 if usage is None else usage.files_read,
        provider_id=provider.provider_id,
        model_id=provider.configuration.model_id,
        provider_counters=counters,
        confidence=None if selection is None else selection.confidence,
        warnings=warnings,
        provenance=None if selection is None else selection.provenance,
        fallback_used=(
            selection is not None
            and selection.provenance == "deterministic_fallback"
        ),
        context_bytes=0 if usage is None else usage.context_bytes,
        expectations=expectations,
        budgets=budgets,
        failure=failure,
    )


def _evaluate_expectations(
    task: BenchmarkTask,
    mode: BenchmarkMode,
    selected_files: tuple[str, ...],
    warning_codes: tuple[str, ...],
) -> BenchmarkExpectationEvaluation:
    selected = set(selected_files)
    required = _effective(task, mode, "required_files_all")
    groups = _effective(task, mode, "required_files_any")
    forbidden = _effective(task, mode, "forbidden_files")
    allowed_warnings = set(_effective(task, mode, "allowed_warnings"))
    required_warnings = set(_effective(task, mode, "required_warnings"))
    observed_warnings = set(warning_codes)
    any_groups = tuple(
        BenchmarkAnyFileExpectation(
            files=group,
            matched_files=tuple(path for path in group if path in selected),
            passed=any(path in selected for path in group),
        )
        for group in groups
    )
    missing_required = tuple(path for path in required if path not in selected)
    selected_forbidden = tuple(path for path in forbidden if path in selected)
    unexpected_warnings = tuple(sorted(observed_warnings - allowed_warnings))
    missing_warnings = tuple(sorted(required_warnings - observed_warnings))
    passed = not (
        missing_required
        or selected_forbidden
        or unexpected_warnings
        or missing_warnings
        or any(not group.passed for group in any_groups)
    )
    return BenchmarkExpectationEvaluation(
        missing_required_files=missing_required,
        any_file_groups=any_groups,
        selected_forbidden_files=selected_forbidden,
        expected_facets=_effective(task, mode, "expected_facets"),
        unexpected_warnings=unexpected_warnings,
        missing_required_warnings=missing_warnings,
        passed=passed,
    )


def _evaluate_budgets(
    task: BenchmarkTask,
    mode: BenchmarkMode,
    *,
    selected_files: int,
    files_read: int,
    counters: BenchmarkProviderCounters,
) -> BenchmarkBudgetEvaluation:
    selected = _limit(_effective(task, mode, "max_selected_files"), selected_files)
    reads = _limit(_effective(task, mode, "max_files_read"), files_read)
    generations = _limit(
        _effective(task, mode, "max_model_generations"),
        counters.model_generations,
    )
    http_limit = _effective(task, mode, "max_provider_http_calls")
    http = (
        None
        if http_limit is None
        else _limit(http_limit, counters.total_provider_http_calls)
    )
    return BenchmarkBudgetEvaluation(
        selected_files=selected,
        files_read=reads,
        model_generations=generations,
        provider_http_calls=http,
        passed=all(
            item.passed
            for item in (selected, reads, generations, http)
            if item is not None
        ),
    )


def _limit(limit: int, actual: int) -> BenchmarkLimitEvaluation:
    return BenchmarkLimitEvaluation(limit=limit, actual=actual, passed=actual <= limit)


def _effective(task: BenchmarkTask, mode: BenchmarkMode, field: str) -> Any:
    override = getattr(task.mode_overrides, mode.value)
    if override is not None and field in override.model_fields_set:
        value = getattr(override, field)
        if value is not None:
            return value
    return getattr(task, field)


def _failure(
    code: str,
    error: Exception,
    message: str | None = None,
) -> BenchmarkFailure:
    safe_message = (message or str(error) or code).replace("\x00", "")[:2_000]
    return BenchmarkFailure(
        code=code[:200],
        error_type=type(error).__name__[:200],
        message=safe_message or code,
    )


__all__ = ["run_discovery_benchmark"]
