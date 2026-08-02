"""Deterministic quality and repeatability metrics for benchmark run DTOs."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable
from itertools import combinations
from typing import Literal

from contextforge.benchmarks.models import (
    BenchmarkCohortMetrics,
    BenchmarkConfidenceSummary,
    BenchmarkDurationPercentiles,
    BenchmarkDurationSummary,
    BenchmarkIntegerRange,
    BenchmarkPairwiseJaccard,
    BenchmarkRunResult,
)


def calculate_benchmark_metrics(
    runs: tuple[BenchmarkRunResult, ...],
) -> tuple[BenchmarkCohortMetrics, ...]:
    """Aggregate runs only when every comparison identity component matches.

    Failed and cancelled runs are counted but excluded. Pairwise rates, Jaccard,
    and warning stability are ``None`` with fewer than two complete runs. Quality
    rates use configured required files, forbidden-file opportunities, and facets
    as their denominators and are ``None`` when the corresponding expectation is
    empty. Duration percentiles use nearest-rank and require two complete runs.
    """

    grouped: dict[tuple[object, ...], list[BenchmarkRunResult]] = defaultdict(list)
    for position, run in enumerate(runs):
        # A complete result with unknown repository/configuration identity cannot be
        # safely compared, even with another result carrying the same unknown.
        unknown_identity = run.status == "complete" and (
            run.source_snapshot_digest is None
            or run.effective_configuration_digest is None
        )
        key = (
            run.task_id,
            run.repository_path,
            run.mode.value,
            run.source_snapshot_digest,
            run.index_generation_id,
            run.effective_configuration_digest,
            position if unknown_identity else -1,
        )
        grouped[key].append(run)
    return tuple(
        _cohort(tuple(grouped[key]))
        for key in sorted(grouped, key=lambda item: tuple(str(value) for value in item))
    )


def _cohort(runs: tuple[BenchmarkRunResult, ...]) -> BenchmarkCohortMetrics:
    ordered = tuple(sorted(runs, key=lambda run: run.repetition))
    complete = tuple(run for run in ordered if run.status == "complete")
    pairs = tuple(combinations(complete, 2))
    jaccard = tuple(
        BenchmarkPairwiseJaccard(
            first_repetition=first.repetition,
            second_repetition=second.repetition,
            similarity=_jaccard(first.selected_files, second.selected_files),
        )
        for first, second in pairs
    )
    pair_count = len(pairs)
    stability_kind: Literal[
        "insufficient_data",
        "semantic_stability",
        "deterministic_fallback_repeatability",
    ]
    if pair_count == 0:
        stability_kind = "insufficient_data"
    elif all(run.provenance == "deterministic_fallback" for run in complete):
        stability_kind = "deterministic_fallback_repeatability"
    else:
        # Agreement between model-backed runs demonstrates stability, not proof of
        # deterministic execution.
        stability_kind = "semantic_stability"
    first = ordered[0]
    return BenchmarkCohortMetrics(
        task_id=first.task_id,
        repository_path=first.repository_path,
        mode=first.mode,
        source_snapshot_digest=first.source_snapshot_digest,
        index_generation_id=first.index_generation_id,
        effective_configuration_digest=first.effective_configuration_digest,
        total_run_count=len(ordered),
        complete_run_count=len(complete),
        excluded_run_count=len(ordered) - len(complete),
        comparable_pair_count=pair_count,
        stability_kind=stability_kind,
        exact_selected_file_match_rate=_pair_rate(
            pairs,
            lambda first_run, second_run: (
                set(first_run.selected_files) == set(second_run.selected_files)
            ),
        ),
        exact_ordered_match_rate=_pair_rate(
            pairs,
            lambda first_run, second_run: (
                first_run.selected_files == second_run.selected_files
            ),
        ),
        required_file_recall=_quality_rate(
            complete,
            lambda run: len(run.expectations.matched_required_files),
            lambda run: len(run.expectations.required_files),
        ),
        forbidden_file_selection_rate=_quality_rate(
            complete,
            lambda run: len(run.expectations.selected_forbidden_files),
            lambda run: len(run.expectations.forbidden_files),
        ),
        expected_facet_coverage_rate=_quality_rate(
            complete,
            lambda run: len(run.expectations.covered_expected_facets),
            lambda run: len(run.expectations.expected_facets),
        ),
        pairwise_jaccard=jaccard,
        mean_jaccard_similarity=(
            None
            if not jaccard
            else sum(item.similarity for item in jaccard) / len(jaccard)
        ),
        warning_stability=_pair_rate(
            pairs,
            lambda first_run, second_run: (
                _warning_set(first_run) == _warning_set(second_run)
            ),
        ),
        fallback_rate=(
            None
            if not complete
            else sum(run.fallback_used for run in complete) / len(complete)
        ),
        confidence=_confidence(complete),
        duration=_duration(complete),
        files_read_range=_integer_range(tuple(run.files_read for run in complete)),
        model_call_range=_integer_range(
            tuple(run.provider_counters.model_calls for run in complete)
        ),
    )


def _pair_rate(
    pairs: tuple[tuple[BenchmarkRunResult, BenchmarkRunResult], ...],
    matches: Callable[[BenchmarkRunResult, BenchmarkRunResult], bool],
) -> float | None:
    if not pairs:
        return None
    return sum(matches(first, second) for first, second in pairs) / len(pairs)


def _quality_rate(
    runs: tuple[BenchmarkRunResult, ...],
    numerator: Callable[[BenchmarkRunResult], int],
    denominator: Callable[[BenchmarkRunResult], int],
) -> float | None:
    total = sum(denominator(run) for run in runs)
    if total == 0:
        return None
    return sum(numerator(run) for run in runs) / total


def _jaccard(first: tuple[str, ...], second: tuple[str, ...]) -> float:
    first_set = set(first)
    second_set = set(second)
    union = first_set | second_set
    return 1.0 if not union else len(first_set & second_set) / len(union)


def _warning_set(run: BenchmarkRunResult) -> set[str]:
    return {warning.model_dump_json() for warning in run.warnings}


def _confidence(
    runs: tuple[BenchmarkRunResult, ...],
) -> BenchmarkConfidenceSummary | None:
    values = tuple(run.confidence for run in runs if run.confidence is not None)
    if not values:
        return None
    minimum = min(values)
    maximum = max(values)
    return BenchmarkConfidenceSummary(
        mean=sum(values) / len(values),
        minimum=minimum,
        maximum=maximum,
        spread=maximum - minimum,
    )


def _duration(
    runs: tuple[BenchmarkRunResult, ...],
) -> BenchmarkDurationSummary | None:
    values = tuple(sorted(run.duration_ms for run in runs))
    if not values:
        return None
    percentiles = (
        None
        if len(values) < 2
        else BenchmarkDurationPercentiles(
            p50_ms=_nearest_rank(values, 0.50),
            p90_ms=_nearest_rank(values, 0.90),
            p95_ms=_nearest_rank(values, 0.95),
        )
    )
    return BenchmarkDurationSummary(
        mean_ms=sum(values) / len(values),
        percentiles=percentiles,
    )


def _nearest_rank(values: tuple[int, ...], percentile: float) -> int:
    return values[max(0, math.ceil(percentile * len(values)) - 1)]


def _integer_range(values: tuple[int, ...]) -> BenchmarkIntegerRange | None:
    if not values:
        return None
    return BenchmarkIntegerRange(minimum=min(values), maximum=max(values))


__all__ = ["calculate_benchmark_metrics"]
