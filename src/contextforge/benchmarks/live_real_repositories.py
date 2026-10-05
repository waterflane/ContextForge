"""Opt-in pinned repository benchmark using the actual Index v3.1 pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from contextforge.application import build_repository_index
from contextforge.benchmarks.answers import (
    OrdinaryBaselineContextOverflow,
    run_paired_answer_regression,
)
from contextforge.benchmarks.dispatch_budget import BenchmarkDispatchBudget
from contextforge.benchmarks.models import BenchmarkSourceRange
from contextforge.benchmarks.real_repositories import (
    CandidateSelectionDiagnostic,
    EvidenceLossReason,
    EvidenceSelectionTransition,
    RealBenchmarkBuildReport,
    RealBenchmarkMode,
    RealBenchmarkObservation,
    RealBenchmarkPhaseUsage,
    RealBenchmarkTask,
    RealBenchmarkTaskReport,
    RealRepositoryBenchmarkManifest,
    RealRepositoryBenchmarkReport,
    _ExternalRepositoryUnavailable,
    _skipped,
    aggregate_real_repository_report,
    evaluate_real_repository_observation,
    temporary_read_only_clone,
)
from contextforge.context import (
    CompiledContextCapsule,
    ContextBudget,
    RepresentationMode,
    compile_context_capsule,
)
from contextforge.intelligence.cards import SemanticCardBuildResult
from contextforge.intelligence.file_policy import FILE_POLICY_REGISTRY
from contextforge.intelligence.indexer import load_file_code_map
from contextforge.intelligence.retrieval import (
    RetrievalResult,
    _all_structural_postings,
    load_retrieval_index,
    retrieve_context_candidates,
)
from contextforge.intelligence.semantic_lexicon import callable_symbols
from contextforge.intelligence.store import load_manifest
from contextforge.models import (
    ContextWindowExceededError,
    ModelProvider,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ProviderCapabilities,
    ProviderCircuitOpenError,
    ProviderConfiguration,
    ProviderConfigurationError,
    estimate_request_context,
)
from contextforge.project_config import create_model_provider


class _MeasuredProvider:
    """Count actual HTTP attempts and provider-reported tokens by phase."""

    def __init__(
        self, provider: ModelProvider, budget: BenchmarkDispatchBudget | None = None
    ) -> None:
        self._provider = provider
        self.configuration = provider.configuration
        self.calls = 0
        self.reported_input: int | None = None
        self.reported_output: int | None = None
        self.estimated_input = 0
        self.records: list[RealBenchmarkPhaseUsage] = []
        self.budget = budget
        if budget is not None and (
            self.configuration.retry_limit
            or self.configuration.max_json_repair_attempts
        ):
            raise ValueError("bounded live dispatch requires zero retries and repairs")

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
        started = time.perf_counter()
        calls_before = self.calls
        estimated = estimate_request_context(
            request, self.configuration
        ).estimated_input_tokens
        if self.budget is not None:
            self.budget.authorize(request.operation_id, estimated)
        self.estimated_input += estimated
        reported_in: int | None = None
        reported_out: int | None = None
        error: str | None = None
        try:
            pending = self._provider.complete_structured(
                request, cancellation=cancellation
            )
            if self.budget is None:
                response = await pending
            else:
                response = await asyncio.wait_for(
                    pending, timeout=self.budget.remaining_seconds
                )
        except TimeoutError as exc:
            if self.budget is None:
                raise
            error = "time_limit"
            self.calls += 1
            assert self.budget is not None
            self.budget.stop_reasons.append("time_limit")
            raise ModelProviderError("benchmark time limit reached") from exc
        except ModelProviderError as exc:
            error = type(exc).__name__
            self.calls += (
                0
                if isinstance(
                    exc, (ProviderCircuitOpenError, ProviderConfigurationError)
                )
                or (
                    isinstance(exc, ContextWindowExceededError)
                    and exc.budget is not None
                )
                else exc.total_provider_http_calls
            )
            raise
        else:
            self.calls += (
                response.diagnostic.total_provider_http_calls
                if response.diagnostic is not None
                else 1
            )
            if response.usage is not None:
                reported_in = response.usage.input_tokens
                reported_out = response.usage.output_tokens
                if reported_in is not None:
                    self.reported_input = (self.reported_input or 0) + reported_in
                if reported_out is not None:
                    self.reported_output = (self.reported_output or 0) + reported_out
            return response
        finally:
            if self.budget is not None:
                self.budget.record(
                    request.operation_id, self.calls - calls_before, estimated
                )
            self.records.append(
                RealBenchmarkPhaseUsage(
                    phase=request.operation_id,
                    provider_calls=self.calls - calls_before,
                    estimated_input_tokens=estimated,
                    reported_input_tokens=reported_in,
                    reported_output_tokens=reported_out,
                    duration_ms=round((time.perf_counter() - started) * 1000),
                    error=error,
                )
            )

    async def close(self) -> None:
        await self._provider.close()


def _new_provider(configuration: ProviderConfiguration) -> ModelProvider:
    return create_model_provider(configuration)


def _fresh_process_reloads(root: Path, task: str, *, attempts: int) -> int:
    script = (
        "import asyncio,json,sys;"
        "from contextforge.intelligence.retrieval import retrieve_context_candidates;"
        "r=asyncio.run(retrieve_context_candidates(sys.argv[1],sys.argv[2],limit=5));"
        "print(json.dumps([r.generation_id,"
        "[c.model_dump(mode='json') for c in r.candidates]],"
        "sort_keys=True,separators=(',',':')))"
    )
    expected: str | None = None
    successes = 0
    for seed in range(attempts):
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script, str(root), task],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
        )
        if completed.returncode != 0:
            break
        value = completed.stdout.strip()
        if expected is None:
            expected = value
        if value != expected:
            break
        successes += 1
    return successes


def _materialized(
    compiled: object,
) -> tuple[tuple[str, ...], tuple[BenchmarkSourceRange, ...]]:
    from contextforge.context import CompiledContextCapsule

    if not isinstance(compiled, CompiledContextCapsule):
        raise TypeError("compiled capsule expected")
    materials = (*compiled.capsule.working_set, *compiled.capsule.task_context)
    paths = tuple(dict.fromkeys(item.path for item in materials))
    ranges: list[BenchmarkSourceRange] = []
    for item in materials:
        if item.representation is RepresentationMode.SLICE:
            ranges.extend(
                BenchmarkSourceRange(
                    path=item.path,
                    start_line=value.start_line,
                    end_line=value.end_line,
                )
                for value in item.ranges
            )
        elif item.representation is RepresentationMode.FULL:
            line_count = len(item.content.splitlines())
            if line_count:
                ranges.append(
                    BenchmarkSourceRange(
                        path=item.path, start_line=1, end_line=line_count
                    )
                )
    return paths, tuple(ranges)


def _validate_reviewed_sources(root: Path, task: RealBenchmarkTask) -> None:
    """Refuse stale manually reviewed line addresses before any answer calls."""

    for span in (*task.required_ranges, *task.oracle_ranges):
        source = root / span.path
        if not source.is_file() or span.end_line > len(
            source.read_text(encoding="utf-8").splitlines()
        ):
            raise ValueError(f"reviewed range is stale: {span.path}")
    active = load_manifest(root)
    reference = (
        active.artifacts.semantic_retrieval or active.artifacts.structural_retrieval
    )
    if reference is None:
        raise ValueError("reviewed evidence has no retrieval index")
    index = load_retrieval_index(root, reference, manifest=active)
    documents = {item.path: item for item in index.documents}
    for assertion in task.answer_assertions:
        for support in assertion.support:
            document = documents.get(support.citation.path)
            if document is None:
                raise ValueError("reviewed evidence references an absent source")
            identities = {
                item.evidence_id: item.source_range
                for item in document.positional_postings
            }
            code_map = load_file_code_map(root, support.citation.path, manifest=active)
            identities.update(
                (item.evidence_id, item.source_range)
                for item in _all_structural_postings(code_map)
            )
            identities.update(
                (item.evidence_id, item.source_range)
                for claim in document.semantic_claims
                for item in claim.evidence
                if item.source_range is not None
            )
            if any(
                evidence_id not in identities
                or identities[evidence_id].end_line < support.citation.start_line
                or identities[evidence_id].start_line > support.citation.end_line
                for evidence_id in support.material_evidence_ids
            ):
                raise ValueError("reviewed evidence ID is stale or outside its range")


def _failure_reason(error: Exception) -> str:
    """Keep report reasons bounded without persisting model or source text."""

    if isinstance(error, ValueError):
        detail = str(error)
        if isinstance(error, OrdinaryBaselineContextOverflow):
            return "ordinary_baseline_context_overflow"
        if detail.startswith("reviewed range is stale:"):
            return "reviewed_range_stale"
        if detail == "reviewed evidence references an absent source":
            return "reviewed_source_absent"
        if detail == "reviewed evidence ID is stale or outside its range":
            return "reviewed_evidence_stale"
        if detail == "reviewed evidence has no retrieval index":
            return "retrieval_index_absent"
    return f"pipeline_error:{type(error).__name__}"


def _candidate_diagnostics(
    retrieval: RetrievalResult,
) -> tuple[CandidateSelectionDiagnostic, ...]:
    return tuple(
        CandidateSelectionDiagnostic(
            candidate_id=item.candidate_id,
            path=item.path,
            source_sha256=item.source_sha256,
            rank=rank,
            exact_group=item.exact_group,
            bm25_field_scores=item.bm25_field_scores,
            selection_reasons=item.selection_reasons,
            evidence_ids=tuple(
                sorted({e.evidence_id for e in item.evidence_ranges if e.evidence_id})
            ),
            graph_paths=tuple(sorted({n.path for n in item.graph_neighbors})),
        )
        for rank, item in enumerate(retrieval.candidates[:64], 1)
    )


def _evidence_transitions(
    root: Path,
    task: RealBenchmarkTask,
    retrieval: RetrievalResult,
    compiled: CompiledContextCapsule,
    ranges: tuple[BenchmarkSourceRange, ...],
) -> tuple[EvidenceSelectionTransition, ...]:
    """Classify losses after evaluation; never pass these addresses to models."""
    candidates = {item.path: item for item in retrieval.candidates}
    materials = {
        item.path: item
        for item in (*compiled.capsule.working_set, *compiled.capsule.task_context)
    }
    planned = (
        {}
        if retrieval.evidence_plan is None
        else {item.path: item for item in retrieval.evidence_plan.items}
    )
    results = []
    for span in task.required_ranges:
        candidate = candidates.get(span.path)
        material = materials.get(span.path)
        item = planned.get(span.path)
        evidence_ids = tuple(
            sorted(
                {
                    e.evidence_id
                    for e in (() if candidate is None else candidate.evidence_ranges)
                    if e.evidence_id
                    and e.source_range.start_line <= span.start_line
                    and e.source_range.end_line >= span.end_line
                }
            )
        )
        actual_ids = () if material is None else material.evidence_ids
        current = candidate is None or (
            (root / span.path).is_file()
            and hashlib.sha256((root / span.path).read_bytes()).hexdigest()
            == candidate.source_sha256
        )
        covered = any(
            r.path == span.path
            and r.start_line <= span.start_line
            and r.end_line >= span.end_line
            for r in ranges
        )
        reason: EvidenceLossReason = (
            "source_stale"
            if not current
            else "covered"
            if covered
            else "absent_from_pool"
            if candidate is None
            else "range_not_found"
            if not evidence_ids
            else "budget_excluded"
            if item is not None and material is None
            else "selection_lost"
        )
        results.append(
            EvidenceSelectionTransition(
                source_range=span,
                candidate_id=None if candidate is None else candidate.candidate_id,
                retrieval_evidence_ids=evidence_ids,
                planned_evidence_ids=() if item is None else item.evidence_ids,
                materialized_evidence_ids=tuple(
                    sorted(set(evidence_ids) & set(actual_ids))
                ),
                source_current=current,
                reason=reason,
            )
        )
    return tuple(results)


async def _evaluate_task(
    root: Path,
    task: RealBenchmarkTask,
    mode: RealBenchmarkMode,
    provider: _MeasuredProvider,
    *,
    repetition: int,
    model_repetition: int = 1,
    budget: ContextBudget,
) -> RealBenchmarkTaskReport:
    usage_start = len(provider.records)
    _validate_reviewed_sources(root, task)
    started = time.perf_counter()
    retrieval_started = time.perf_counter()
    query_stages: dict[str, float] = {}
    retrieval = await retrieve_context_candidates(
        root,
        task.task,
        limit=20,
        query_stage_timings_ms=query_stages,
        provider=provider if mode is RealBenchmarkMode.PLANNED else None,
        planning_mode="auto" if mode is RealBenchmarkMode.PLANNED else "off",
    )
    retrieval_ms = round((time.perf_counter() - retrieval_started) * 1_000)
    compiler_started = time.perf_counter()
    compiled = compile_context_capsule(root, task.task, retrieval, budget=budget)
    compiler_ms = round((time.perf_counter() - compiler_started) * 1000)
    paths, ranges = _materialized(compiled)
    phase_errors: tuple[str, ...] = ()
    try:
        paired = await run_paired_answer_regression(
            root,
            task.task,
            task.answer_assertions,
            task.oracle_ranges,
            compiled,
            provider,
            ordinary_paths=task.required_files,
        )
    except (ModelProviderError, ValueError) as exc:
        paired = None
        phase_errors = (f"final_answer:{_failure_reason(exc)}",)
    planning = retrieval.planning_diagnostics
    sufficiency = compiled.compilation_sufficiency
    observation = RealBenchmarkObservation(
        mode=mode,
        compiler_ms=compiler_ms,
        query_stage_timings_ms=query_stages,
        candidate_diagnostics=_candidate_diagnostics(retrieval),
        evidence_transitions=_evidence_transitions(
            root, task, retrieval, compiled, ranges
        ),
        retrieved_top5=tuple(item.path for item in retrieval.candidates[:5]),
        materialized_files=paths,
        materialized_ranges=ranges,
        material_evidence_ids={
            material.path: material.evidence_ids
            for material in (
                *compiled.capsule.working_set,
                *compiled.capsule.task_context,
            )
        },
        capsule_tokens=compiled.token_count,
        ordinary_tokens=(
            paired.ordinary.input_tokens
            if paired is not None and paired.ordinary is not None
            else 0
        ),
        planner_calls=retrieval.provider_calls,
        planner_input_tokens=0 if planning is None else planning.input_tokens,
        planner_output_tokens=0 if planning is None else planning.output_tokens,
        planner_estimated_input_tokens=0
        if planning is None
        else planning.estimated_input_tokens,
        planner_reported_input_tokens=None
        if planning is None
        else planning.reported_input_tokens,
        planner_reported_output_tokens=None
        if planning is None
        else planning.reported_output_tokens,
        planner_status=None if planning is None else planning.status,
        planner_messages=() if planning is None else planning.messages,
        latency_ms=round((time.perf_counter() - started) * 1_000),
        deterministic_warm_query_ms=(
            retrieval_ms if mode is RealBenchmarkMode.DETERMINISTIC else None
        ),
        agentic_planner_ms=(
            retrieval_ms if mode is RealBenchmarkMode.PLANNED else None
        ),
        final_answer_ms=(
            paired.contextforge.duration_ms if paired is not None else None
        ),
        final_answer_provider_calls=(
            paired.contextforge.provider_http_calls if paired is not None else None
        ),
        final_answer_estimated_input_tokens=(
            paired.contextforge.estimated_input_tokens if paired is not None else None
        ),
        final_answer_reported_input_tokens=(
            paired.contextforge.provider_input_tokens if paired is not None else None
        ),
        phase_errors=phase_errors,
        compilation_sufficiency=compiled.compilation_sufficiency,
        materialization_coverage=compiled.coverage_ledger,
        plan_sufficient=(
            None
            if mode is RealBenchmarkMode.DETERMINISTIC
            else sufficiency is not None
            and sufficiency.effective_status == "sufficient"
        ),
        paired_answer=paired,
    )
    evaluated = evaluate_real_repository_observation(
        task, observation, repetition=repetition, model_repetition=model_repetition
    )
    return evaluated.model_copy(
        update={"phase_usage": tuple(provider.records[usage_start:])}
    )


async def run_pinned_real_repository_benchmark(
    manifest: RealRepositoryBenchmarkManifest,
    sources: Mapping[str, str | Path],
    configuration: ProviderConfiguration,
    *,
    repetitions: int = 3,
    model_repetitions: int = 3,
    report_path: str | Path | None = None,
    budget: ContextBudget | None = None,
    hash_seed_reloads: int = 100,
    semantic_max_files: int | None = None,
    semantic_max_requests: int = 4_096,
    dispatch_budget: BenchmarkDispatchBudget | None = None,
    progress: Callable[[str], None] | None = None,
) -> RealRepositoryBenchmarkReport:
    """Build once per clone, then evaluate both retrieval modes and paired answers."""

    if manifest.schema_version != 2:
        raise ValueError("official live benchmark requires a reviewed v2 manifest")
    if repetitions < 1 or model_repetitions < 1:
        raise ValueError("repetitions must be positive")
    if hash_seed_reloads < 0 or hash_seed_reloads > 100:
        raise ValueError("hash-seed reload count must be between 0 and 100")
    if semantic_max_files is not None and semantic_max_files < 1:
        raise ValueError("semantic file limit must be positive")
    if semantic_max_requests < 1:
        raise ValueError("semantic request limit must be positive")
    if configuration.provider_id not in {"openai-compatible", "codex"}:
        raise ValueError("official live benchmark requires an approved model provider")
    effective_budget = budget or ContextBudget(
        context_window_tokens=configuration.context_window,
        response_tokens=1_024,
        safety_margin_tokens=configuration.context_safety_margin,
    )
    runs: list[RealBenchmarkTaskReport] = []
    builds: list[RealBenchmarkBuildReport] = []
    completed_phases: list[dict[str, object]] = []

    def announce(message: str) -> None:
        if progress is not None:
            progress(message)

    def checkpoint() -> None:
        if report_path is None:
            return
        destination = Path(report_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_suffix(destination.suffix + ".tmp")
        staging.write_text(
            json.dumps(
                {
                    "dispatch_stop_reasons": []
                    if dispatch_budget is None
                    else dispatch_budget.stop_reasons,
                    "dispatch_calls": None
                    if dispatch_budget is None
                    else dispatch_budget.calls,
                    "dispatch_estimated_input_tokens": None
                    if dispatch_budget is None
                    else dispatch_budget.estimated_input_tokens,
                    "status": "partial",
                    "acceptance": "unverified",
                    "suite_name": manifest.suite_name,
                    "requested_clone_repetitions": repetitions,
                    "requested_model_repetitions": model_repetitions,
                    "builds": [b.model_dump(mode="json") for b in builds],
                    "runs": [r.model_dump(mode="json") for r in runs],
                    "completed_phases": completed_phases,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        staging.replace(destination)

    checkpoint()
    for repetition in range(1, repetitions + 1):
        for repository in manifest.repositories:
            announce(f"{repository.repository_id} repeat {repetition}: cloning")
            tasks = tuple(
                task
                for task in manifest.tasks
                if task.repository_id == repository.repository_id
            )
            source = sources.get(repository.repository_id)
            if source is None:
                builds.append(
                    RealBenchmarkBuildReport(
                        repository_id=repository.repository_id,
                        repetition=repetition,
                        status="skipped",
                        reason="external_source_missing",
                    )
                )
                runs.extend(
                    _skipped(
                        task,
                        mode,
                        "external_source_missing",
                        repetition=repetition,
                        model_repetition=model_repetition,
                    )
                    for model_repetition in range(1, model_repetitions + 1)
                    for task in tasks
                    for mode in RealBenchmarkMode
                )
                continue
            try:
                with temporary_read_only_clone(source, repository.revision) as root:
                    provider = _MeasuredProvider(
                        _new_provider(configuration), dispatch_budget
                    )
                    try:
                        announce(
                            f"{repository.repository_id} repeat {repetition}: "
                            "structural"
                        )
                        structural_start = time.perf_counter()
                        structural = await build_repository_index(
                            root,
                            provider=None,
                            provider_configuration=None,
                            semantic_scope="none",
                        )
                        structural_ms = round(
                            (time.perf_counter() - structural_start) * 1_000
                        )
                        completed_phases.append(
                            {
                                "repository_id": repository.repository_id,
                                "clone_repetition": repetition,
                                "phase": "structural_index",
                                "duration_ms": structural_ms,
                                "provider_calls": 0,
                            }
                        )
                        checkpoint()
                        semantic_start = time.perf_counter()
                        announce(
                            f"{repository.repository_id} repeat {repetition}: semantic"
                        )
                        report = await build_repository_index(
                            root,
                            provider=provider,
                            provider_configuration=configuration,
                            semantic_scope="all",
                            max_files=(
                                semantic_max_files
                                if semantic_max_files is not None
                                else max(len(structural.snapshot.files), 1)
                            ),
                            semantic_max_requests=semantic_max_requests,
                            semantic_max_input_tokens=50_000_000,
                        )
                        semantic_ms = round(
                            (time.perf_counter() - semantic_start) * 1_000
                        )
                        semantic_calls = provider.calls
                        semantic_estimated_input = provider.estimated_input
                        reported_input = provider.reported_input
                        reported_output = provider.reported_output
                        completed_phases.append(
                            {
                                "repository_id": repository.repository_id,
                                "clone_repetition": repetition,
                                "phase": "semantic_index",
                                "duration_ms": semantic_ms,
                                "provider_calls": semantic_calls,
                                "estimated_input_tokens": semantic_estimated_input,
                                "reported_input_tokens": reported_input,
                                "reported_output_tokens": reported_output,
                                "partial": report.partial,
                            }
                        )
                        checkpoint()
                        noop_ms: int | None = None
                        noop_calls: int | None = None
                        noop_generation_unchanged: bool | None = None
                        noop_reanalyzed: int | None = None
                        if not report.partial:
                            noop_start = time.perf_counter()
                            before_noop_calls = provider.calls
                            noop = await build_repository_index(
                                root,
                                provider=provider,
                                provider_configuration=configuration,
                                update_only=True,
                                semantic_scope="all",
                                max_files=(
                                    semantic_max_files
                                    if semantic_max_files is not None
                                    else max(len(structural.snapshot.files), 1)
                                ),
                                semantic_max_requests=semantic_max_requests,
                                semantic_max_input_tokens=50_000_000,
                            )
                            noop_ms = round((time.perf_counter() - noop_start) * 1_000)
                            noop_calls = provider.calls - before_noop_calls
                            noop_reanalyzed = len(noop.structural.extracted_paths)
                            noop_generation_unchanged = (
                                noop.manifest.generation_id
                                == report.manifest.generation_id
                            )
                        requested = sum(
                            len(callable_symbols(code_map))
                            for code_map in report.structural.code_maps
                            if not FILE_POLICY_REGISTRY.requires_deterministic_card(
                                code_map
                            )
                        )
                        cards = (
                            report.semantic.cards
                            if isinstance(report.semantic, SemanticCardBuildResult)
                            else ()
                        )
                        described = sum(
                            len(card.lexicon.functions)
                            for card in cards
                            if card.lexicon is not None
                        )
                        file_only = sum(
                            len(card.lexicon.functions)
                            for card in cards
                            if card.lexicon is not None
                            and card.lexicon.context_mode == "file_only"
                        )
                        retrieval_reference = (
                            report.manifest.artifacts.semantic_retrieval
                            or report.manifest.artifacts.structural_retrieval
                        )
                        if retrieval_reference is None:
                            raise ValueError("reviewed evidence has no retrieval index")
                        indexed_documents = load_retrieval_index(
                            root, retrieval_reference, manifest=report.manifest
                        ).documents
                        code_test_documents = tuple(
                            document
                            for document in indexed_documents
                            if FILE_POLICY_REGISTRY.profile(document.path)
                            in {"code", "test"}
                        )
                        tagged_documents = sum(
                            bool(document.file_tags) for document in code_test_documents
                        )
                        model_tagged_documents = sum(
                            any(tag.provenance == "model" for tag in document.file_tags)
                            for document in code_test_documents
                        )
                        failed_paths = (
                            set(report.semantic.failed_paths)
                            if report.semantic is not None
                            else set()
                        )
                        failure_codes = Counter(
                            diagnostic.code
                            for card in cards
                            if card.path in failed_paths
                            for diagnostic in card.diagnostics
                            if diagnostic.code != "semantic_scheduler_value"
                        )
                        source_bytes = sum(
                            item.size_bytes for item in report.snapshot.files
                        )
                        artifacts = tuple(
                            path
                            for path in (
                                root
                                / ".contextforge"
                                / "index"
                                / "generations"
                                / report.manifest.generation_id
                            ).rglob("*")
                            if path.is_file()
                        )
                        artifact_bytes = sum(path.stat().st_size for path in artifacts)
                        reload_successes = (
                            _fresh_process_reloads(
                                root, tasks[0].task, attempts=hash_seed_reloads
                            )
                            if tasks
                            else 0
                        )
                        builds.append(
                            RealBenchmarkBuildReport(
                                repository_id=repository.repository_id,
                                repetition=repetition,
                                status="partial" if report.partial else "complete",
                                phase_usage=tuple(provider.records),
                                reason=(
                                    "semantic_files_failed" if report.partial else None
                                ),
                                failed_paths=(
                                    ()
                                    if report.semantic is None
                                    else report.semantic.failed_paths
                                ),
                                failure_code_counts=dict(sorted(failure_codes.items())),
                                semantic_coverage=report.semantic.coverage
                                if isinstance(report.semantic, SemanticCardBuildResult)
                                else None,
                                cold_structural_ms=structural_ms,
                                semantic_offline_ms=semantic_ms,
                                semantic_provider_calls=semantic_calls,
                                semantic_estimated_input_tokens=(
                                    semantic_estimated_input
                                ),
                                semantic_reported_input_tokens=reported_input,
                                semantic_reported_output_tokens=reported_output,
                                requested_functions=requested,
                                described_functions=described,
                                file_only_functions=file_only,
                                code_test_files=len(code_test_documents),
                                tagged_code_test_files=tagged_documents,
                                model_tagged_code_test_files=model_tagged_documents,
                                noop_update_ms=noop_ms,
                                noop_provider_calls=noop_calls,
                                noop_generation_unchanged=noop_generation_unchanged,
                                noop_structurally_reanalyzed_files=noop_reanalyzed,
                                active_amplification=(
                                    artifact_bytes / source_bytes
                                    if source_bytes
                                    else 0.0
                                ),
                                maximum_shard_bytes=max(
                                    (path.stat().st_size for path in artifacts),
                                    default=0,
                                ),
                                fresh_process_reload_successes=reload_successes,
                            )
                        )
                        for model_repetition in range(1, model_repetitions + 1):
                            for task in tasks:
                                for mode in RealBenchmarkMode:
                                    announce(
                                        f"{repository.repository_id} "
                                        f"clone {repetition}, "
                                        f"model {model_repetition}: "
                                        f"{task.task_id} {mode.value}"
                                    )
                                    usage_start = len(provider.records)
                                    try:
                                        runs.append(
                                            await _evaluate_task(
                                                root,
                                                task,
                                                mode,
                                                provider,
                                                repetition=repetition,
                                                model_repetition=model_repetition,
                                                budget=effective_budget,
                                            )
                                        )
                                    except (
                                        OSError,
                                        ValueError,
                                        ModelProviderError,
                                    ) as exc:
                                        runs.append(
                                            _skipped(
                                                task,
                                                mode,
                                                _failure_reason(exc),
                                                repetition=repetition,
                                                model_repetition=model_repetition,
                                            ).model_copy(
                                                update={
                                                    "phase_usage": tuple(
                                                        provider.records[usage_start:]
                                                    )
                                                }
                                            )
                                        )
                                    checkpoint()
                    finally:
                        await provider.close()
            except _ExternalRepositoryUnavailable:
                builds.append(
                    RealBenchmarkBuildReport(
                        repository_id=repository.repository_id,
                        repetition=repetition,
                        status="skipped",
                        reason="external_source_unavailable",
                    )
                )
                runs.extend(
                    _skipped(
                        task,
                        mode,
                        "external_source_unavailable",
                        repetition=repetition,
                        model_repetition=model_repetition,
                    )
                    for model_repetition in range(1, model_repetitions + 1)
                    for task in tasks
                    for mode in RealBenchmarkMode
                )
            except Exception as exc:
                reason = f"pipeline_error:{type(exc).__name__}"
                if not any(
                    build.repository_id == repository.repository_id
                    and build.repetition == repetition
                    for build in builds
                ):
                    builds.append(
                        RealBenchmarkBuildReport(
                            repository_id=repository.repository_id,
                            repetition=repetition,
                            status="partial",
                            reason=reason,
                        )
                    )
                completed = {
                    (run.task_id, run.mode, run.repetition, run.model_repetition)
                    for run in runs
                }
                runs.extend(
                    _skipped(
                        task,
                        mode,
                        reason,
                        repetition=repetition,
                        model_repetition=model_repetition,
                    )
                    for model_repetition in range(1, model_repetitions + 1)
                    for task in tasks
                    for mode in RealBenchmarkMode
                    if (task.task_id, mode, repetition, model_repetition)
                    not in completed
                )
            checkpoint()
    result = aggregate_real_repository_report(
        manifest, tuple(runs), verified_pipeline=True, builds=tuple(builds)
    )
    endpoint = urlsplit(configuration.endpoint)
    host = endpoint.hostname or ""
    if endpoint.port is not None:
        host = f"{host}:{endpoint.port}"
    safe_endpoint = urlunsplit((endpoint.scheme, host, endpoint.path, "", ""))
    result = result.model_copy(
        update={
            "dispatch_stop_reasons": ()
            if dispatch_budget is None
            else tuple(dispatch_budget.stop_reasons),
            "dispatch_calls": None
            if dispatch_budget is None
            else dispatch_budget.calls,
            "dispatch_estimated_input_tokens": None
            if dispatch_budget is None
            else dispatch_budget.estimated_input_tokens,
            "provider_endpoint": safe_endpoint,
            "model_id": configuration.model_id,
            "requested_context_window": configuration.context_window,
            "reasoning_effort": configuration.reasoning_effort,
        }
    )

    if report_path is not None:
        Path(report_path).write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result


__all__ = ["run_pinned_real_repository_benchmark"]
