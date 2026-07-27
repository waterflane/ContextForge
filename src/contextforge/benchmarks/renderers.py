"""Pure text, Markdown, and canonical JSON benchmark result renderers."""

from __future__ import annotations

import json

from contextforge.benchmarks.models import (
    BenchmarkCohortMetrics,
    BenchmarkIntegerRange,
    BenchmarkResult,
    BenchmarkRunResult,
)


def render_benchmark_json(result: BenchmarkResult) -> str:
    """Render the canonical automation representation."""

    return (
        json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )


def render_benchmark_text(result: BenchmarkResult) -> str:
    """Render an interactive, prose-free benchmark summary."""

    lines = [
        "ContextForge discovery benchmark",
        f"Suite: {result.suite_name}",
        f"Overall result: {'PASS' if result.passed else 'FAIL'}",
        f"Runs: {len(result.runs)}",
        "Task and mode summaries:",
    ]
    for metric in result.metrics:
        lines.append(
            f"  {_label(metric)}: {metric.complete_run_count}/"
            f"{metric.total_run_count} complete"
        )
        for run in _cohort_runs(result, metric):
            selected = ", ".join(run.selected_files) or "(none)"
            lines.append(
                f"    repeat {run.repetition}: {run.status}; selected: {selected}"
            )
    lines.extend(("Overall quality:", *_quality_lines(result.metrics, "  ")))
    lines.extend(("Repeatability:", *_repeatability_lines(result.metrics, "  ")))
    lines.extend(("Performance:", *_performance_lines(result.metrics, "  ")))
    lines.append("Warnings:")
    warning_lines = _warning_lines(result.runs, "  ")
    lines.extend(warning_lines or ("  (none)",))
    lines.append("Failed expectations:")
    failure_lines = _failure_lines(result.runs, "  ")
    lines.extend(failure_lines or ("  (none)",))
    return "\n".join(lines) + "\n"


def render_benchmark_markdown(result: BenchmarkResult) -> str:
    """Render a reviewable Markdown benchmark report."""

    lines = [
        "# ContextForge discovery benchmark",
        "",
        f"- Suite: `{result.suite_name}`",
        f"- Overall result: **{'PASS' if result.passed else 'FAIL'}**",
        f"- Runs: {len(result.runs)}",
        "",
        "## Task and mode summaries",
        "",
        "| Task | Mode | Complete | Total | Stability |",
        "|---|---:|---:|---:|---|",
    ]
    lines.extend(
        f"| `{metric.task_id}` | {metric.mode.value} | "
        f"{metric.complete_run_count} | {metric.total_run_count} | "
        f"{metric.stability_kind} |"
        for metric in result.metrics
    )
    lines.extend(("", "## Overall quality", ""))
    lines.extend(f"- {line.strip()}" for line in _quality_lines(result.metrics, ""))
    lines.extend(("", "## Repeatability", ""))
    lines.extend(
        f"- {line.strip()}" for line in _repeatability_lines(result.metrics, "")
    )
    lines.extend(("", "## Performance", ""))
    lines.extend(
        f"- {line.strip()}" for line in _performance_lines(result.metrics, "")
    )
    lines.extend(("", "## Warnings", ""))
    warnings = _warning_lines(result.runs, "")
    lines.extend((f"- {line}" for line in warnings) if warnings else ("(none)",))
    lines.extend(("", "## Failed expectations", ""))
    failures = _failure_lines(result.runs, "")
    lines.extend((f"- {line}" for line in failures) if failures else ("(none)",))
    return "\n".join(lines) + "\n"


def _quality_lines(
    metrics: tuple[BenchmarkCohortMetrics, ...], prefix: str
) -> tuple[str, ...]:
    return tuple(
        f"{prefix}{_label(metric)}: required recall "
        f"{_rate(metric.required_file_recall)}; forbidden selection "
        f"{_rate(metric.forbidden_file_selection_rate)}; facet coverage "
        f"{_rate(metric.expected_facet_coverage_rate)}"
        for metric in metrics
    ) or (f"{prefix}(none)",)


def _repeatability_lines(
    metrics: tuple[BenchmarkCohortMetrics, ...], prefix: str
) -> tuple[str, ...]:
    return tuple(
        f"{prefix}{_label(metric)}: {metric.stability_kind}; exact set "
        f"{_rate(metric.exact_selected_file_match_rate)}; exact order "
        f"{_rate(metric.exact_ordered_match_rate)}; mean Jaccard "
        f"{_rate(metric.mean_jaccard_similarity)}; warning stability "
        f"{_rate(metric.warning_stability)}; fallback "
        f"{_rate(metric.fallback_rate)}"
        for metric in metrics
    ) or (f"{prefix}(none)",)


def _performance_lines(
    metrics: tuple[BenchmarkCohortMetrics, ...], prefix: str
) -> tuple[str, ...]:
    lines: list[str] = []
    for metric in metrics:
        duration = metric.duration
        percentiles = None if duration is None else duration.percentiles
        duration_text = (
            "n/a"
            if duration is None
            else f"mean {duration.mean_ms:.1f} ms"
            + (
                "; percentiles n/a"
                if percentiles is None
                else (
                    f"; p50/p90/p95 {percentiles.p50_ms}/"
                    f"{percentiles.p90_ms}/{percentiles.p95_ms} ms"
                )
            )
        )
        lines.append(
            f"{prefix}{_label(metric)}: duration {duration_text}; files read "
            f"{_range(metric.files_read_range)}; model calls "
            f"{_range(metric.model_call_range)}"
        )
    return tuple(lines) or (f"{prefix}(none)",)


def _warning_lines(
    runs: tuple[BenchmarkRunResult, ...], prefix: str
) -> tuple[str, ...]:
    return tuple(
        f"{prefix}{run.task_id} [{run.mode.value}] repeat {run.repetition}: "
        f"{warning.code} - {warning.message}"
        for run in runs
        for warning in run.warnings
    )


def _failure_lines(
    runs: tuple[BenchmarkRunResult, ...], prefix: str
) -> tuple[str, ...]:
    lines: list[str] = []
    for run in runs:
        label = f"{prefix}{run.task_id} [{run.mode.value}] repeat {run.repetition}"
        if run.status != "complete":
            code = "failed" if run.failure is None else run.failure.code
            lines.append(f"{label}: task {run.status} ({code})")
        evaluation = run.expectations
        if evaluation.missing_required_files:
            lines.append(
                f"{label}: missing required files: "
                + ", ".join(evaluation.missing_required_files)
            )
        for group in evaluation.any_file_groups:
            if not group.passed:
                lines.append(f"{label}: required any of: " + ", ".join(group.files))
        if evaluation.selected_forbidden_files:
            lines.append(
                f"{label}: selected forbidden files: "
                + ", ".join(evaluation.selected_forbidden_files)
            )
        if evaluation.missing_expected_facets:
            lines.append(
                f"{label}: missing expected facets: "
                + ", ".join(evaluation.missing_expected_facets)
            )
        if evaluation.unexpected_warnings:
            lines.append(
                f"{label}: unexpected warnings: "
                + ", ".join(evaluation.unexpected_warnings)
            )
        if evaluation.missing_required_warnings:
            lines.append(
                f"{label}: missing required warnings: "
                + ", ".join(evaluation.missing_required_warnings)
            )
        for name, limit in (
            ("selected files", run.budgets.selected_files),
            ("files read", run.budgets.files_read),
            ("model generations", run.budgets.model_generations),
            ("provider HTTP calls", run.budgets.provider_http_calls),
        ):
            if limit is not None and not limit.passed:
                lines.append(
                    f"{label}: {name} budget {limit.actual}>{limit.limit}"
                )
    return tuple(lines)


def _cohort_runs(
    result: BenchmarkResult, metric: BenchmarkCohortMetrics
) -> tuple[BenchmarkRunResult, ...]:
    return tuple(
        run
        for run in result.runs
        if run.task_id == metric.task_id
        and run.repository_path == metric.repository_path
        and run.mode == metric.mode
        and run.source_snapshot_digest == metric.source_snapshot_digest
        and run.index_generation_id == metric.index_generation_id
        and run.effective_configuration_digest == metric.effective_configuration_digest
    )


def _label(metric: BenchmarkCohortMetrics) -> str:
    return f"{metric.task_id} [{metric.mode.value}]"


def _rate(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1%}"


def _range(value: BenchmarkIntegerRange | None) -> str:
    if value is None:
        return "n/a"
    return (
        str(value.minimum)
        if value.minimum == value.maximum
        else f"{value.minimum}-{value.maximum}"
    )


__all__ = [
    "render_benchmark_json",
    "render_benchmark_markdown",
    "render_benchmark_text",
]
