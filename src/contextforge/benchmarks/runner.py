"""Application-layer execution of validated discovery benchmark manifests."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from contextforge.application import build_discovery_request, suggest_repository_context
from contextforge.benchmarks.metrics import calculate_benchmark_metrics
from contextforge.benchmarks.models import (
    BenchmarkAnyFileExpectation,
    BenchmarkBudgetEvaluation,
    BenchmarkExpectationEvaluation,
    BenchmarkExpectations,
    BenchmarkFailure,
    BenchmarkLimitEvaluation,
    BenchmarkManifest,
    BenchmarkMode,
    BenchmarkProviderCounters,
    BenchmarkResult,
    BenchmarkRunResult,
    BenchmarkTask,
)
from contextforge.discovery import (
    DiscoveryCandidate,
    DiscoveryError,
    DiscoveryRequest,
    DiscoveryRunRecord,
)
from contextforge.intelligence import (
    IndexManifestNotFoundError,
    IndexManifestReadError,
    calculate_source_snapshot_digest,
    load_manifest,
)
from contextforge.models import ModelProvider
from contextforge.progress import ProgressEvent, ProgressObserver
from contextforge.repositories import scan_repository


class _BenchmarkPreconditionError(RuntimeError):
    """Raised when a task's declared benchmark fixture state is unavailable."""


class _RunMetrics:
    """Collect non-domain metrics exposed by the normal application workflow."""

    def __init__(self, progress: ProgressObserver | None) -> None:
        self.files_considered = 0
        self.progress = progress

    def observe(self, event: ProgressEvent) -> None:
        value = event.metadata.get("structural_files")
        if type(value) is int:
            self.files_considered = max(self.files_considered, value)
        if self.progress is not None:
            self.progress(event)


async def run_discovery_benchmark(
    manifest: BenchmarkManifest,
    repository_root: str | Path,
    provider: ModelProvider,
    *,
    clock: Callable[[], float] = time.monotonic,
    progress: ProgressObserver | None = None,
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
                        progress=progress,
                    )
                )
    canonical_runs = tuple(runs)
    return BenchmarkResult(
        manifest_schema_version=manifest.schema_version,
        suite_name=manifest.suite_name,
        runs=canonical_runs,
        metrics=calculate_benchmark_metrics(canonical_runs),
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
    progress: ProgressObserver | None,
) -> BenchmarkRunResult:
    metrics = _RunMetrics(progress)
    started = clock()
    run_record: DiscoveryRunRecord | None = None
    failure: BenchmarkFailure | None = None
    configuration_digest: str | None = None
    expectations_evaluated = True
    try:
        request = build_discovery_request(
            task=task.task,
            mode=mode.value,
            includes=_effective(task, mode, "include_paths"),
            excludes=_effective(task, mode, "exclude_paths"),
        )
        configuration_digest = _configuration_digest(task, mode, provider, request)
        repository = root.joinpath(*task.repository_path.split("/")).resolve()
        repository.relative_to(root)
        with _prepared_repository(repository, task, mode) as prepared:
            run_record = await suggest_repository_context(
                prepared,
                provider,
                request,
                progress=metrics.observe,
                persist_diagnostics=False,
            )
    except _BenchmarkPreconditionError as exc:
        expectations_evaluated = False
        failure = _failure("benchmark_precondition_failed", exc)
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
        configuration_digest,
        expectations_evaluated=expectations_evaluated,
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
    configuration_digest: str | None,
    *,
    expectations_evaluated: bool = True,
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
        model_calls=0 if usage is None else usage.model_calls,
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
    candidates = () if selection is None else selection.selected
    expectations = (
        _evaluate_expectations(
            task,
            mode,
            selected_files,
            candidates,
            tuple(item.code for item in warnings),
        )
        if expectations_evaluated
        else _unevaluated_expectations(task, mode)
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
        source_snapshot_digest=(None if run is None else run.source_snapshot_digest),
        index_generation_id=None if run is None else run.index_generation_id,
        effective_configuration_digest=configuration_digest,
        provider_id=provider.provider_id,
        model_id=provider.configuration.model_id,
        provider_counters=counters,
        confidence=None if selection is None else selection.confidence,
        warnings=warnings,
        provenance=None if selection is None else selection.provenance,
        fallback_used=(
            selection is not None and selection.provenance == "deterministic_fallback"
        ),
        context_bytes=0 if usage is None else usage.context_bytes,
        expectations=expectations,
        budgets=budgets,
        failure=failure,
    )


@contextmanager
def _prepared_repository(
    repository: Path,
    task: BenchmarkTask,
    mode: BenchmarkMode,
) -> Iterator[Path]:
    precondition = task.index_precondition
    if mode is BenchmarkMode.FRESH or precondition is None:
        yield repository
        return
    if precondition.kind == "clean":
        _require_index_drift(repository, ())
        yield repository
        return

    drift_path = precondition.drift_path
    if drift_path is None:  # Defended by manifest validation.
        raise _BenchmarkPreconditionError(
            "isolated stale-index fixture has no drift path"
        )
    try:
        snapshot = scan_repository(repository)
        with tempfile.TemporaryDirectory(prefix="contextforge-benchmark-") as temporary:
            isolated = Path(temporary) / "repository"
            for source_file in snapshot.files:
                source = repository.joinpath(*source_file.path.split("/"))
                destination = isolated.joinpath(*source_file.path.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            index = repository / ".contextforge" / "index"
            shutil.copytree(index, isolated / ".contextforge" / "index")

            existing_drift = _source_index_drift(isolated)
            if not existing_drift:
                target = isolated.joinpath(*drift_path.split("/"))
                if not target.is_file():
                    raise _BenchmarkPreconditionError(
                        f"isolated stale-index drift path is unavailable: {drift_path}"
                    )
                target.write_bytes(
                    target.read_bytes() + b"\ncontextforge benchmark stale fixture\n"
                )
            _require_index_drift(isolated, (drift_path,))
            yield isolated
    except _BenchmarkPreconditionError:
        raise
    except (IndexManifestNotFoundError, IndexManifestReadError, OSError) as exc:
        raise _BenchmarkPreconditionError(
            f"isolated stale-index fixture preparation failed: {exc}"
        ) from exc


def _require_index_drift(repository: Path, expected: tuple[str, ...]) -> None:
    try:
        observed = _source_index_drift(repository)
    except (IndexManifestNotFoundError, IndexManifestReadError, OSError) as exc:
        raise _BenchmarkPreconditionError(
            f"indexed snapshot precondition could not be verified: {exc}"
        ) from exc
    if observed != expected:
        expected_label = ", ".join(expected) or "none"
        observed_label = ", ".join(observed) or "none"
        raise _BenchmarkPreconditionError(
            "indexed snapshot precondition failed; "
            f"expected source/index drift [{expected_label}], "
            f"observed [{observed_label}]"
        )


def _source_index_drift(repository: Path) -> tuple[str, ...]:
    snapshot = scan_repository(repository)
    manifest = load_manifest(repository)
    current = {item.path: item for item in snapshot.files}
    indexed = {item.path: item for item in manifest.files}
    drift = set(current) ^ set(indexed)
    for path in set(current) & set(indexed):
        source = current[path]
        state = indexed[path]
        if (
            source.sha256 != state.source_sha256
            or source.size_bytes != state.source_size_bytes
            or source.language != state.language
        ):
            drift.add(path)
    observed = tuple(sorted(drift))
    if (
        not observed
        and manifest.build.source_snapshot_digest
        != calculate_source_snapshot_digest(snapshot)
    ):
        raise _BenchmarkPreconditionError(
            "indexed snapshot digest does not match otherwise-current source records"
        )
    return observed


def _unevaluated_expectations(
    task: BenchmarkTask,
    mode: BenchmarkMode,
) -> BenchmarkExpectationEvaluation:
    """Retain configured expectations without reporting discovery misses."""

    return BenchmarkExpectationEvaluation(
        required_files=_effective(task, mode, "required_files_all"),
        any_file_groups=tuple(
            BenchmarkAnyFileExpectation(files=group, matched_files=(), passed=True)
            for group in _effective(task, mode, "required_files_any")
        ),
        forbidden_files=_effective(task, mode, "forbidden_files"),
        expected_facets=_effective(task, mode, "expected_facets"),
        passed=True,
    )


def _evaluate_expectations(
    task: BenchmarkTask,
    mode: BenchmarkMode,
    selected_files: tuple[str, ...],
    candidates: tuple[DiscoveryCandidate, ...],
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
    matched_required = tuple(path for path in required if path in selected)
    missing_required = tuple(path for path in required if path not in selected)
    selected_forbidden = tuple(path for path in forbidden if path in selected)
    expected_facets = _effective(task, mode, "expected_facets")
    covered_facets = tuple(
        facet for facet in expected_facets if _facet_covered(facet, candidates)
    )
    missing_facets = tuple(
        facet for facet in expected_facets if facet not in covered_facets
    )
    unexpected_warnings = tuple(sorted(observed_warnings - allowed_warnings))
    missing_warnings = tuple(sorted(required_warnings - observed_warnings))
    passed = not (
        missing_required
        or selected_forbidden
        or unexpected_warnings
        or missing_warnings
        or missing_facets
        or any(not group.passed for group in any_groups)
    )
    return BenchmarkExpectationEvaluation(
        required_files=required,
        matched_required_files=matched_required,
        missing_required_files=missing_required,
        any_file_groups=any_groups,
        forbidden_files=forbidden,
        selected_forbidden_files=selected_forbidden,
        expected_facets=expected_facets,
        covered_expected_facets=covered_facets,
        missing_expected_facets=missing_facets,
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


def _configuration_digest(
    task: BenchmarkTask,
    mode: BenchmarkMode,
    provider: ModelProvider,
    request: DiscoveryRequest,
) -> str:
    expectations = {
        field: _effective(task, mode, field)
        for field in sorted(BenchmarkExpectations.model_fields)
    }
    encoded = json.dumps(
        {
            "benchmark": expectations,
            "discovery_request": request.model_dump(mode="json"),
            "provider": provider.configuration.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _facet_covered(
    facet: str,
    candidates: tuple[DiscoveryCandidate, ...],
) -> bool:
    expected = _metric_tokens(facet)
    observed: set[str] = set()
    for candidate in candidates:
        observed.update(_metric_tokens(candidate.path or ""))
        observed.update(_metric_tokens(candidate.reason.summary))
        observed.update(_metric_tokens(candidate.reason.discovery_source))
        for evidence in candidate.reason.evidence:
            observed.update(_metric_tokens(evidence))
    return bool(expected) and expected <= observed


def _metric_tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold().replace("_", " ")))


def _failure(
    code: str,
    error: Exception,
    message: str | None = None,
) -> BenchmarkFailure:
    if isinstance(error, DiscoveryError) and message:
        safe_message = message.replace("\x00", "")[:2_000]
    elif isinstance(error, _BenchmarkPreconditionError):
        safe_message = "Benchmark repository/index precondition was not satisfied."
    else:
        # Arbitrary exception text may contain source, provider payloads, secrets,
        # or host-private absolute paths. The typed code and exception class retain
        # enough diagnostic identity without publishing that untrusted material.
        safe_message = "Benchmark run failed before discovery completed."
    return BenchmarkFailure(
        code=code[:200],
        error_type=type(error).__name__[:200],
        message=safe_message,
    )


__all__ = ["run_discovery_benchmark"]
