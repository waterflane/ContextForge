"""Application-layer execution of validated discovery benchmark manifests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic_core import to_jsonable_python

from contextforge.application import (
    build_discovery_request,
    build_repository_index,
    suggest_repository_context,
)
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
    BenchmarkPipeline,
    BenchmarkProviderCounters,
    BenchmarkRangeCoverage,
    BenchmarkResult,
    BenchmarkRunResult,
    BenchmarkSourceRange,
    BenchmarkTask,
)
from contextforge.context import (
    ConservativeTokenEstimator,
    ContextBudget,
    ContextCapsule,
    RepresentationMode,
    compile_context_capsule,
)
from contextforge.discovery import (
    DiscoveryCandidate,
    DiscoveryError,
    DiscoveryRequest,
    DiscoveryRunRecord,
)
from contextforge.intelligence import (
    CandidateCard,
    IndexManifest,
    IndexManifestNotFoundError,
    IndexManifestReadError,
    calculate_source_snapshot_digest,
    load_manifest,
    load_semantic_card,
    retrieve_context_candidates,
)
from contextforge.models import (
    ModelProvider,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ProviderConfiguration,
)
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


class _CountingModelProvider:
    """Retain actual safe provider accounting without changing provider behavior."""

    def __init__(self, provider: ModelProvider) -> None:
        self._provider = provider
        self.configuration: ProviderConfiguration = provider.configuration
        self.model_calls = 0
        self.model_generations = 0
        self.repair_generations = 0
        self.provider_discovery_calls = 0
        self.provider_capability_calls = 0
        self.transport_attempts = 0
        self.total_provider_http_calls = 0

    @property
    def provider_id(self) -> str:
        return self._provider.provider_id

    def capabilities(self) -> ProviderCapabilities:
        return self._provider.capabilities()

    async def complete_structured(
        self,
        request: ModelRequest,
        *,
        cancellation: asyncio.Event | None = None,
    ) -> ModelResponse:
        self.model_calls += 1
        try:
            response = await self._provider.complete_structured(
                request, cancellation=cancellation
            )
        except ModelProviderError as exc:
            self.provider_discovery_calls += exc.provider_discovery_calls
            self.provider_capability_calls += exc.provider_capability_calls
            self.transport_attempts += exc.transport_attempts
            self.total_provider_http_calls += exc.total_provider_http_calls
            if exc.diagnostic is not None:
                self.model_generations += exc.diagnostic.model_generations
                self.repair_generations += exc.diagnostic.repair_generations
            raise
        diagnostic = response.diagnostic
        if diagnostic is None:
            self.model_generations += 1
            self.transport_attempts += 1
            self.total_provider_http_calls += 1
        else:
            self.model_generations += diagnostic.model_generations
            self.repair_generations += diagnostic.repair_generations
            self.provider_discovery_calls += diagnostic.provider_discovery_calls
            self.provider_capability_calls += diagnostic.provider_capability_calls
            self.transport_attempts += diagnostic.transport_attempts
            self.total_provider_http_calls += diagnostic.total_provider_http_calls
        return response

    async def close(self) -> None:
        """The benchmark borrows the caller-owned provider and never closes it."""

        return None


@dataclass(frozen=True)
class _SelectionMeasurements:
    selected_ranges: tuple[BenchmarkSourceRange, ...] = ()
    selected_line_count: int = 0
    useful_line_count: int = 0
    selected_tokens: int = 0
    useful_tokens: int = 0
    semantic_claims: int = 0
    ungrounded_claims: int = 0
    grounded_claims: int = 0
    dropped_claims: int = 0


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
    if task.pipeline is BenchmarkPipeline.INDEX_V3_CAPSULE:
        return await _run_index_v3_once(
            root,
            task,
            mode,
            repetition,
            provider,
            clock=clock,
            progress=progress,
        )
    metrics = _RunMetrics(progress)
    started = clock()
    run_record: DiscoveryRunRecord | None = None
    failure: BenchmarkFailure | None = None
    configuration_digest: str | None = None
    expectations_evaluated = True
    measurements = _SelectionMeasurements()
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
            measurements = _measure_selection(
                prepared,
                ()
                if run_record.final_selection is None
                else run_record.final_selection.selected,
                _effective(task, mode, "required_ranges"),
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
        measurements,
        expectations_evaluated=expectations_evaluated,
    )


async def _run_index_v3_once(
    root: Path,
    task: BenchmarkTask,
    mode: BenchmarkMode,
    repetition: int,
    provider: ModelProvider,
    *,
    clock: Callable[[], float],
    progress: ProgressObserver | None,
) -> BenchmarkRunResult:
    """Exercise build/update, persisted retrieval, and Capsule v2 materialization."""

    outer_started = clock()
    operation_started = outer_started
    failure: BenchmarkFailure | None = None
    expectations_evaluated = True
    configuration_digest: str | None = None
    selected_files: tuple[str, ...] = ()
    files_considered = 0
    files_read = 0
    context_bytes = 0
    source_snapshot_digest: str | None = None
    generation_id: str | None = None
    candidates: tuple[CandidateCard, ...] = ()
    measurements = _SelectionMeasurements()
    counters = BenchmarkProviderCounters()
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
        with _prepared_index_v3_repository(repository, task, mode) as prepared:
            operation_started = clock()
            counting_provider = _CountingModelProvider(provider)
            semantic_requests = 0
            semantic_repairs = 0
            diff_paths: tuple[str, ...] = ()
            if mode is BenchmarkMode.FRESH:
                report = await build_repository_index(
                    prepared,
                    provider=counting_provider,
                    provider_configuration=provider.configuration,
                    progress=progress,
                )
                manifest = report.manifest
                if report.semantic is not None:
                    semantic_requests, semantic_repairs = _semantic_request_counts(
                        report.semantic
                    )
            elif mode is BenchmarkMode.HYBRID:
                diff_paths = (_controlled_change_path(prepared, task, mode),)
                report = await build_repository_index(
                    prepared,
                    provider=counting_provider,
                    provider_configuration=provider.configuration,
                    update_only=True,
                    progress=progress,
                )
                manifest = report.manifest
                if report.semantic is not None:
                    semantic_requests, semantic_repairs = _semantic_request_counts(
                        report.semantic
                    )
            else:
                manifest = load_manifest(prepared)
            retrieval = await retrieve_context_candidates(
                prepared,
                task.task,
                manifest=manifest,
                diff_paths=diff_paths,
            )
            compiled = compile_context_capsule(
                prepared,
                task.task,
                retrieval,
                manifest=manifest,
                budget=ContextBudget(
                    context_window_tokens=32_768,
                    response_tokens=4_096,
                    safety_margin_tokens=1_024,
                ),
            )
            candidates = tuple(
                item
                for item in retrieval.candidates
                if item.path
                in {
                    material.path
                    for material in (
                        *compiled.capsule.working_set,
                        *compiled.capsule.task_context,
                    )
                }
            )
            selected_files = tuple(
                material.path
                for material in (
                    *compiled.capsule.working_set,
                    *compiled.capsule.task_context,
                )
            )
            files_considered = len(retrieval.candidates)
            files_read = sum(
                material.representation
                in {RepresentationMode.SLICE, RepresentationMode.FULL}
                for material in (
                    *compiled.capsule.working_set,
                    *compiled.capsule.task_context,
                )
            )
            context_bytes = len(compiled.prompt.encode("utf-8"))
            source_snapshot_digest = manifest.build.source_snapshot_digest
            generation_id = manifest.generation_id
            measurements = _measure_capsule(
                prepared,
                compiled.capsule,
                candidates,
                _effective(task, mode, "required_ranges"),
                manifest=manifest,
            )
            request_calls = max(
                counting_provider.model_calls,
                semantic_requests + retrieval.provider_calls,
            )
            counters = BenchmarkProviderCounters(
                model_calls=request_calls,
                model_generations=max(
                    counting_provider.model_generations - semantic_repairs, 0
                ),
                repair_generations=(
                    counting_provider.repair_generations + semantic_repairs
                ),
                auxiliary_provider_calls=(
                    counting_provider.provider_discovery_calls
                    + counting_provider.provider_capability_calls
                    + retrieval.provider_calls
                ),
                provider_discovery_calls=counting_provider.provider_discovery_calls,
                provider_capability_calls=counting_provider.provider_capability_calls,
                transport_attempts=counting_provider.transport_attempts,
                total_provider_http_calls=(counting_provider.total_provider_http_calls),
            )
    except _BenchmarkPreconditionError as exc:
        expectations_evaluated = False
        failure = _failure("benchmark_precondition_failed", exc)
    except Exception as exc:
        failure = _failure("application_error", exc)
    duration_ms = max(0, round((clock() - operation_started) * 1_000))
    expectations = (
        _evaluate_expectations(
            task,
            mode,
            selected_files,
            candidates,
            (),
            measurements,
        )
        if expectations_evaluated
        else _unevaluated_expectations(task, mode)
    )
    budgets = _evaluate_budgets(
        task,
        mode,
        selected_files=len(selected_files),
        files_read=files_read,
        counters=counters,
    )
    status: Literal["complete", "failed"] = "failed" if failure else "complete"
    return BenchmarkRunResult(
        task_id=task.task_id,
        repository_path=task.repository_path,
        pipeline=task.pipeline,
        mode=mode,
        repetition=repetition,
        status=status,
        passed=status == "complete" and expectations.passed and budgets.passed,
        duration_ms=duration_ms,
        selected_files=selected_files,
        files_considered=max(files_considered, len(selected_files)),
        files_read=files_read,
        source_snapshot_digest=source_snapshot_digest,
        index_generation_id=generation_id,
        effective_configuration_digest=configuration_digest,
        provider_id=provider.provider_id,
        model_id=provider.configuration.model_id,
        provider_counters=counters,
        provenance="index_v3_deterministic",
        context_bytes=context_bytes,
        selected_ranges=measurements.selected_ranges,
        selected_tokens=measurements.selected_tokens,
        useful_tokens=measurements.useful_tokens,
        semantic_claims=measurements.semantic_claims,
        ungrounded_claims=measurements.ungrounded_claims,
        grounded_claims=measurements.grounded_claims,
        dropped_claims=measurements.dropped_claims,
        latency_kind=_latency_kind(mode),
        expectations=expectations,
        budgets=budgets,
        failure=failure,
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
    measurements: _SelectionMeasurements,
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
            measurements,
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
        pipeline=task.pipeline,
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
        selected_ranges=measurements.selected_ranges,
        selected_tokens=measurements.selected_tokens,
        useful_tokens=measurements.useful_tokens,
        semantic_claims=measurements.semantic_claims,
        ungrounded_claims=measurements.ungrounded_claims,
        grounded_claims=(measurements.semantic_claims - measurements.ungrounded_claims),
        dropped_claims=measurements.ungrounded_claims,
        latency_kind=_latency_kind(mode),
        expectations=expectations,
        budgets=budgets,
        failure=failure,
    )


def _latency_kind(
    mode: BenchmarkMode,
) -> Literal["cold", "warm", "incremental"]:
    if mode is BenchmarkMode.FRESH:
        return "cold"
    if mode is BenchmarkMode.INDEXED:
        return "warm"
    return "incremental"


def _measure_selection(
    repository: Path,
    candidates: tuple[DiscoveryCandidate, ...],
    required_ranges: tuple[BenchmarkSourceRange, ...],
) -> _SelectionMeasurements:
    """Measure selected source against declared useful ranges without model calls."""

    estimator = ConservativeTokenEstimator()
    selected: list[BenchmarkSourceRange] = []
    selected_chunks: list[str] = []
    useful_chunks: list[str] = []
    semantic_claims = len(candidates)
    ungrounded_claims = sum(not item.reason.evidence for item in candidates)
    required_by_path: dict[str, list[BenchmarkSourceRange]] = {}
    for item in required_ranges:
        required_by_path.setdefault(item.path, []).append(item)

    for candidate in candidates:
        if candidate.path is None or candidate.kind not in {
            "full_file",
            "line_ranges",
            "related_test",
        }:
            continue
        try:
            text = repository.joinpath(*candidate.path.split("/")).read_text(
                encoding="utf-8"
            )
        except (OSError, UnicodeError):
            continue
        lines = text.splitlines(keepends=True)
        if not lines:
            continue
        ranges = (
            tuple((item.start_line, item.end_line) for item in candidate.ranges)
            if candidate.kind == "line_ranges"
            else ((1, len(lines)),)
        )
        for start_line, end_line in ranges:
            start = max(1, start_line)
            end = min(len(lines), end_line)
            if end < start:
                continue
            selected.append(
                BenchmarkSourceRange(
                    path=candidate.path,
                    start_line=start,
                    end_line=end,
                )
            )
            selected_chunks.append("".join(lines[start - 1 : end]))
            for required in required_by_path.get(candidate.path, ()):
                overlap_start = max(start, required.start_line)
                overlap_end = min(end, required.end_line)
                if overlap_end >= overlap_start:
                    useful_chunks.append(
                        "".join(lines[overlap_start - 1 : overlap_end])
                    )

    canonical = tuple(
        sorted(selected, key=lambda item: (item.path, item.start_line, item.end_line))
    )
    return _SelectionMeasurements(
        selected_ranges=canonical,
        selected_line_count=sum(
            item.end_line - item.start_line + 1 for item in canonical
        ),
        useful_line_count=sum(
            chunk.count("\n") + (not chunk.endswith("\n")) for chunk in useful_chunks
        ),
        selected_tokens=estimator.count("".join(selected_chunks)),
        useful_tokens=estimator.count("".join(useful_chunks)),
        semantic_claims=semantic_claims,
        ungrounded_claims=ungrounded_claims,
    )


def _measure_capsule(
    repository: Path,
    capsule: ContextCapsule,
    candidates: tuple[CandidateCard, ...],
    required_ranges: tuple[BenchmarkSourceRange, ...],
    *,
    manifest: IndexManifest,
) -> _SelectionMeasurements:
    """Measure only material actually present in a compiled Capsule v2."""

    estimator = ConservativeTokenEstimator()
    materials = (*capsule.working_set, *capsule.task_context)
    selected: list[BenchmarkSourceRange] = []
    useful_chunks: list[str] = []
    required_by_path: dict[str, list[BenchmarkSourceRange]] = {}
    for item in required_ranges:
        required_by_path.setdefault(item.path, []).append(item)
    for material in materials:
        if material.representation is RepresentationMode.SLICE:
            ranges = tuple((item.start_line, item.end_line) for item in material.ranges)
        elif material.representation is RepresentationMode.FULL:
            try:
                text = repository.joinpath(*material.path.split("/")).read_text(
                    encoding="utf-8"
                )
            except (OSError, UnicodeError):
                continue
            line_count = len(text.splitlines())
            if line_count == 0:
                continue
            ranges = ((1, line_count),)
        else:
            continue
        try:
            source = repository.joinpath(*material.path.split("/")).read_text(
                encoding="utf-8"
            )
        except (OSError, UnicodeError):
            continue
        lines = source.splitlines(keepends=True)
        for start_line, end_line in ranges:
            selected.append(
                BenchmarkSourceRange(
                    path=material.path,
                    start_line=start_line,
                    end_line=end_line,
                )
            )
            for required in required_by_path.get(material.path, ()):
                overlap_start = max(start_line, required.start_line)
                overlap_end = min(end_line, required.end_line)
                if overlap_end >= overlap_start:
                    useful_chunks.append(
                        "".join(lines[overlap_start - 1 : overlap_end])
                    )
    grounded_claims = 0
    dropped_claims = 0
    for candidate in candidates:
        try:
            card = load_semantic_card(repository, candidate.path, manifest=manifest)
        except (OSError, ValueError):
            continue
        grounded_claims += 1 + len(card.concepts) + len(card.responsibilities)
        grounded_claims += len(card.side_effects) + sum(
            len(values) for values in card.profile_facts.values()
        )
        dropped_claims += sum(item.dropped_items for item in card.diagnostics)
    canonical = tuple(
        sorted(selected, key=lambda item: (item.path, item.start_line, item.end_line))
    )
    return _SelectionMeasurements(
        selected_ranges=canonical,
        selected_line_count=sum(
            item.end_line - item.start_line + 1 for item in canonical
        ),
        useful_line_count=sum(
            chunk.count("\n") + (not chunk.endswith("\n")) for chunk in useful_chunks
        ),
        selected_tokens=sum(item.token_count for item in materials),
        useful_tokens=estimator.count("".join(useful_chunks)),
        semantic_claims=grounded_claims + dropped_claims,
        ungrounded_claims=dropped_claims,
        grounded_claims=grounded_claims,
        dropped_claims=dropped_claims,
    )


def _semantic_request_counts(value: object) -> tuple[int, int]:
    requests = getattr(value, "request_count", 0)
    repairs = getattr(value, "repair_count", 0)
    return (
        requests if type(requests) is int and requests >= 0 else 0,
        repairs if type(repairs) is int and repairs >= 0 else 0,
    )


@contextmanager
def _prepared_index_v3_repository(
    repository: Path,
    task: BenchmarkTask,
    mode: BenchmarkMode,
) -> Iterator[Path]:
    if mode is BenchmarkMode.INDEXED:
        _require_v3_index(repository)
        _require_index_drift(repository, ())
        yield repository
        return
    snapshot = scan_repository(repository)
    try:
        with tempfile.TemporaryDirectory(
            prefix="contextforge-index-v3-benchmark-"
        ) as temporary:
            isolated = Path(temporary) / "repository"
            for source_file in snapshot.files:
                source = repository.joinpath(*source_file.path.split("/"))
                destination = isolated.joinpath(*source_file.path.split("/"))
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            if mode is BenchmarkMode.HYBRID:
                _require_v3_index(repository)
                _require_index_drift(repository, ())
                shutil.copytree(
                    repository / ".contextforge" / "index",
                    isolated / ".contextforge" / "index",
                )
            yield isolated
    except _BenchmarkPreconditionError:
        raise
    except OSError as exc:
        raise _BenchmarkPreconditionError(
            f"Index v3 benchmark fixture preparation failed: {exc}"
        ) from exc


def _require_v3_index(repository: Path) -> None:
    try:
        manifest = load_manifest(repository)
    except (IndexManifestNotFoundError, IndexManifestReadError, OSError) as exc:
        raise _BenchmarkPreconditionError(
            "Index v3 benchmark requires an existing generation."
        ) from exc
    if manifest.schema_version != 3:
        raise _BenchmarkPreconditionError(
            "Index v3 benchmark cannot use an older generation."
        )


def _controlled_change_path(
    repository: Path,
    task: BenchmarkTask,
    mode: BenchmarkMode,
) -> str:
    snapshot = scan_repository(repository)
    available = {item.path for item in snapshot.files}
    preferred = tuple(
        dict.fromkeys(
            (
                *(item.path for item in _effective(task, mode, "required_ranges")),
                *_effective(task, mode, "required_files_all"),
                *_effective(task, mode, "include_paths"),
                *sorted(available),
            )
        )
    )
    try:
        selected_path = next(item for item in preferred if item in available)
    except StopIteration as exc:
        raise _BenchmarkPreconditionError(
            "hybrid Index v3 benchmark has no source file to change"
        ) from exc
    if not isinstance(selected_path, str):
        raise _BenchmarkPreconditionError(
            "hybrid Index v3 benchmark selected an invalid source path"
        )
    target = repository.joinpath(*selected_path.split("/"))
    try:
        target.write_bytes(target.read_bytes() + b"\n")
    except OSError as exc:
        raise _BenchmarkPreconditionError(
            "hybrid Index v3 benchmark could not create controlled source drift"
        ) from exc
    return selected_path


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

    required_ranges = _effective(task, mode, "required_ranges")
    required_files = tuple(
        sorted(
            {
                *_effective(task, mode, "required_files_all"),
                *(item.path for item in required_ranges),
            }
        )
    )
    return BenchmarkExpectationEvaluation(
        required_files=required_files,
        any_file_groups=tuple(
            BenchmarkAnyFileExpectation(files=group, matched_files=(), passed=True)
            for group in _effective(task, mode, "required_files_any")
        ),
        optional_files=_effective(task, mode, "optional_files"),
        forbidden_files=_effective(task, mode, "forbidden_files"),
        expected_facets=_effective(task, mode, "expected_facets"),
        required_ranges=required_ranges,
        passed=True,
    )


def _evaluate_expectations(
    task: BenchmarkTask,
    mode: BenchmarkMode,
    selected_files: tuple[str, ...],
    candidates: tuple[DiscoveryCandidate | CandidateCard, ...],
    warning_codes: tuple[str, ...],
    measurements: _SelectionMeasurements,
) -> BenchmarkExpectationEvaluation:
    selected = set(selected_files)
    required_ranges = _effective(task, mode, "required_ranges")
    required = tuple(
        sorted(
            {
                *_effective(task, mode, "required_files_all"),
                *(item.path for item in required_ranges),
            }
        )
    )
    groups = _effective(task, mode, "required_files_any")
    optional = _effective(task, mode, "optional_files")
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
    relevant = (
        set(required) | set(optional) | {path for group in groups for path in group}
    )
    relevant_selected = tuple(path for path in selected_files if path in relevant)
    irrelevant_selected = tuple(path for path in selected_files if path not in relevant)
    range_coverage = tuple(
        BenchmarkRangeCoverage(
            required_range=required_range,
            covered_lines=_covered_lines(required_range, measurements.selected_ranges),
            required_lines=required_range.end_line - required_range.start_line + 1,
            passed=(
                _covered_lines(required_range, measurements.selected_ranges)
                == required_range.end_line - required_range.start_line + 1
            ),
        )
        for required_range in required_ranges
    )
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
        or any(not item.passed for item in range_coverage)
    )
    return BenchmarkExpectationEvaluation(
        required_files=required,
        matched_required_files=matched_required,
        missing_required_files=missing_required,
        any_file_groups=any_groups,
        optional_files=optional,
        relevant_selected_files=relevant_selected,
        irrelevant_selected_files=irrelevant_selected,
        forbidden_files=forbidden,
        selected_forbidden_files=selected_forbidden,
        expected_facets=expected_facets,
        covered_expected_facets=covered_facets,
        missing_expected_facets=missing_facets,
        required_ranges=required_ranges,
        range_coverage=range_coverage,
        selected_line_count=measurements.selected_line_count,
        useful_line_count=measurements.useful_line_count,
        unexpected_warnings=unexpected_warnings,
        missing_required_warnings=missing_warnings,
        passed=passed,
    )


def _covered_lines(
    required: BenchmarkSourceRange,
    selected: tuple[BenchmarkSourceRange, ...],
) -> int:
    intervals = [
        (
            max(required.start_line, item.start_line),
            min(required.end_line, item.end_line),
        )
        for item in selected
        if item.path == required.path
        and item.end_line >= required.start_line
        and item.start_line <= required.end_line
    ]
    if not intervals:
        return 0
    covered = 0
    current_start, current_end = sorted(intervals)[0]
    for start, end in sorted(intervals)[1:]:
        if start <= current_end + 1:
            current_end = max(current_end, end)
        else:
            covered += current_end - current_start + 1
            current_start, current_end = start, end
    return covered + current_end - current_start + 1


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
            "benchmark": to_jsonable_python(expectations),
            "discovery_request": request.model_dump(mode="json"),
            "pipeline": task.pipeline.value,
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
    candidates: tuple[DiscoveryCandidate | CandidateCard, ...],
) -> bool:
    expected = _metric_tokens(facet)
    observed: set[str] = set()
    for candidate in candidates:
        observed.update(_metric_tokens(candidate.path or ""))
        if isinstance(candidate, CandidateCard):
            observed.update(_metric_tokens(candidate.synopsis))
            for value in (
                *candidate.matched_concepts,
                *candidate.matched_symbols,
                *candidate.provenance,
            ):
                observed.update(_metric_tokens(value))
        else:
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
