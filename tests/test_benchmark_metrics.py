from typing import Literal

import pytest

from contextforge.benchmarks import (
    BenchmarkBudgetEvaluation,
    BenchmarkExpectationEvaluation,
    BenchmarkFailure,
    BenchmarkLimitEvaluation,
    BenchmarkMode,
    BenchmarkProviderCounters,
    BenchmarkRunResult,
    calculate_benchmark_metrics,
)
from contextforge.discovery import CompletenessWarning

SOURCE = "a" * 64
OTHER_SOURCE = "b" * 64
INDEX = "c" * 64
CONFIG = "d" * 64
OTHER_CONFIG = "e" * 64


def _limit() -> BenchmarkLimitEvaluation:
    return BenchmarkLimitEvaluation(limit=10, actual=1, passed=True)


def _run(
    repetition: int,
    selected: tuple[str, ...],
    *,
    matched_required: tuple[str, ...] = ("a.py", "b.py"),
    selected_forbidden: tuple[str, ...] = (),
    covered_facets: tuple[str, ...] = ("alpha", "beta"),
    warning: bool = True,
    fallback: bool = False,
    confidence: float = 0.8,
    duration_ms: int = 20,
    files_read: int = 2,
    model_calls: int = 3,
    source: str = SOURCE,
    index: str | None = INDEX,
    configuration: str = CONFIG,
    status: Literal["complete", "failed"] = "complete",
) -> BenchmarkRunResult:
    complete = status == "complete"
    return BenchmarkRunResult(
        task_id="task",
        repository_path="repository",
        mode=BenchmarkMode.HYBRID,
        repetition=repetition,
        status=status,
        passed=complete,
        duration_ms=duration_ms,
        selected_files=selected if complete else (),
        files_read=files_read if complete else 0,
        source_snapshot_digest=source,
        index_generation_id=index,
        effective_configuration_digest=configuration,
        provider_id="fake",
        model_id="benchmark-v1",
        provider_counters=BenchmarkProviderCounters(
            model_calls=model_calls if complete else 0
        ),
        confidence=confidence if complete else None,
        warnings=(
            (CompletenessWarning(code="stable-warning", message="warning"),)
            if warning and complete
            else ()
        ),
        provenance=(
            "deterministic_fallback" if fallback else "model"
        )
        if complete
        else None,
        fallback_used=fallback if complete else False,
        expectations=BenchmarkExpectationEvaluation(
            required_files=("a.py", "b.py"),
            matched_required_files=matched_required if complete else (),
            missing_required_files=tuple(
                path
                for path in ("a.py", "b.py")
                if path not in matched_required
            ),
            forbidden_files=("x.py", "y.py"),
            selected_forbidden_files=selected_forbidden if complete else (),
            expected_facets=("alpha", "beta"),
            covered_expected_facets=covered_facets if complete else (),
            passed=complete,
        ),
        budgets=BenchmarkBudgetEvaluation(
            selected_files=_limit(),
            files_read=_limit(),
            model_generations=_limit(),
            passed=True,
        ),
        failure=(
            None
            if complete
            else BenchmarkFailure(
                code="failed", error_type="TestFailure", message="failed"
            )
        ),
    )


def test_metrics_are_manually_verifiable_for_three_runs() -> None:
    runs = (
        _run(
            1,
            ("a.py", "b.py"),
            selected_forbidden=("x.py",),
            confidence=0.6,
            duration_ms=10,
            files_read=1,
            model_calls=2,
        ),
        _run(
            2,
            ("b.py", "a.py"),
            covered_facets=("alpha",),
            fallback=True,
            confidence=0.8,
            duration_ms=20,
            files_read=3,
            model_calls=4,
        ),
        _run(
            3,
            ("a.py", "c.py"),
            matched_required=("a.py",),
            selected_forbidden=("y.py",),
            covered_facets=(),
            warning=False,
            confidence=1.0,
            duration_ms=40,
            files_read=2,
            model_calls=3,
        ),
    )

    metric = calculate_benchmark_metrics(runs)[0]

    assert metric.comparable_pair_count == 3
    assert metric.stability_kind == "semantic_stability"
    assert metric.exact_selected_file_match_rate == 1 / 3
    assert metric.exact_ordered_match_rate == 0.0
    assert tuple(item.similarity for item in metric.pairwise_jaccard) == (
        1.0,
        1 / 3,
        1 / 3,
    )
    assert metric.mean_jaccard_similarity == pytest.approx(5 / 9)
    assert metric.required_file_recall == 5 / 6
    assert metric.forbidden_file_selection_rate == 1 / 3
    assert metric.expected_facet_coverage_rate == 0.5
    assert metric.warning_stability == 1 / 3
    assert metric.fallback_rate == 1 / 3
    assert metric.confidence is not None
    assert metric.confidence.mean == pytest.approx(0.8)
    assert metric.confidence.minimum == 0.6
    assert metric.confidence.maximum == 1.0
    assert metric.confidence.spread == pytest.approx(0.4)
    assert metric.duration is not None
    assert metric.duration.mean_ms == pytest.approx(70 / 3)
    assert metric.duration.percentiles is not None
    assert metric.duration.percentiles.model_dump() == {
        "p50_ms": 20,
        "p90_ms": 40,
        "p95_ms": 40,
    }
    assert metric.files_read_range is not None
    assert metric.files_read_range.model_dump() == {"minimum": 1, "maximum": 3}
    assert metric.model_call_range is not None
    assert metric.model_call_range.model_dump() == {"minimum": 2, "maximum": 4}


def test_one_run_defines_quality_but_not_pairwise_repeatability() -> None:
    metric = calculate_benchmark_metrics((_run(1, ("a.py", "b.py")),))[0]

    assert metric.stability_kind == "insufficient_data"
    assert metric.comparable_pair_count == 0
    assert metric.exact_selected_file_match_rate is None
    assert metric.exact_ordered_match_rate is None
    assert metric.pairwise_jaccard == ()
    assert metric.mean_jaccard_similarity is None
    assert metric.warning_stability is None
    assert metric.required_file_recall == 1.0
    assert metric.forbidden_file_selection_rate == 0.0
    assert metric.expected_facet_coverage_rate == 1.0
    assert metric.duration is not None
    assert metric.duration.percentiles is None
    assert metric.files_read_range is not None
    assert metric.files_read_range.minimum == metric.files_read_range.maximum == 2

    repeated_fallback = calculate_benchmark_metrics(
        (
            _run(1, ("a.py",), fallback=True),
            _run(2, ("a.py",), fallback=True),
        )
    )[0]
    assert (
        repeated_fallback.stability_kind
        == "deterministic_fallback_repeatability"
    )


def test_failed_runs_are_excluded_and_identity_changes_split_cohorts() -> None:
    runs = (
        _run(1, ("a.py",)),
        _run(2, (), status="failed"),
        _run(3, ("a.py",), source=OTHER_SOURCE),
        _run(4, ("a.py",), index=None),
        _run(5, ("a.py",), configuration=OTHER_CONFIG),
    )

    metrics = calculate_benchmark_metrics(runs)

    assert len(metrics) == 4
    primary = next(metric for metric in metrics if metric.total_run_count == 2)
    assert primary.complete_run_count == 1
    assert primary.excluded_run_count == 1
    assert primary.comparable_pair_count == 0
    assert primary.required_file_recall == 1.0
    assert all(metric.comparable_pair_count == 0 for metric in metrics)

    failed_only = calculate_benchmark_metrics((_run(1, (), status="failed"),))[0]
    assert failed_only.complete_run_count == 0
    assert failed_only.excluded_run_count == 1
    assert failed_only.required_file_recall is None
    assert failed_only.fallback_rate is None
    assert failed_only.confidence is None
    assert failed_only.duration is None
    assert failed_only.files_read_range is None
    assert failed_only.model_call_range is None
