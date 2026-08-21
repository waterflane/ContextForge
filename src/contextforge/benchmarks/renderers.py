"""Pure text, Markdown, and canonical JSON benchmark result renderers."""

from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass

from contextforge.benchmarks.models import (
    BenchmarkCohortMetrics,
    BenchmarkIntegerRange,
    BenchmarkResult,
    BenchmarkRunResult,
)
from contextforge.discovery.models import CompletenessWarning

_BACKTICK_RUN = re.compile(r"`+")
_MARKDOWN_PUNCTUATION = re.compile(r"([\\`*_{}\[\]<>()#+\-.!|>])")
_REPORT_WIDTH = 88


@dataclass(frozen=True)
class _WarningGroup:
    code: str
    severity: str
    message: str
    record_count: int
    paths: tuple[str, ...]
    related_paths: tuple[str, ...]
    pathless_count: int


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
                f"    repeat {run.repetition}: status {run.status}; "
                f"Benchmark expectations: {_expectation_outcome(run)}; "
                f"Discovery confidence: {_rate(run.confidence)}; selected: {selected}"
            )
    lines.extend(("Overall quality:", *_quality_lines(result.metrics, "  ")))
    lines.extend(("Repeatability:", *_repeatability_lines(result.metrics, "  ")))
    lines.extend(("Performance:", *_performance_lines(result.metrics, "  ")))
    lines.append("Warnings:")
    groups = _run_warning_groups(result.runs)
    grouped_warning_count, affected_path_count = _warning_counts(groups)
    lines.append(
        f"  Grouped warnings: {grouped_warning_count}; "
        f"affected paths: {affected_path_count}"
    )
    warning_lines = _text_warning_lines(groups)
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
    lines.extend(
        (
            "",
            "### Runs",
            "",
            "| Task | Mode | Repeat | Status | Benchmark expectations | "
            "Discovery confidence |",
            "|---|---:|---:|---|---|---:|",
        )
    )
    lines.extend(
        f"| `{run.task_id}` | {run.mode.value} | {run.repetition} | "
        f"{run.status} | {_expectation_outcome(run)} | {_rate(run.confidence)} |"
        for run in result.runs
    )
    lines.extend(("", "## Overall quality", ""))
    lines.extend(f"- {line.strip()}" for line in _quality_lines(result.metrics, ""))
    lines.extend(("", "## Repeatability", ""))
    lines.extend(
        f"- {line.strip()}" for line in _repeatability_lines(result.metrics, "")
    )
    lines.extend(("", "## Performance", ""))
    lines.extend(f"- {line.strip()}" for line in _performance_lines(result.metrics, ""))
    lines.extend(("", "## Warnings", ""))
    groups = _run_warning_groups(result.runs)
    grouped_warning_count, affected_path_count = _warning_counts(groups)
    lines.append(
        f"Grouped warnings: **{grouped_warning_count}**; "
        f"affected paths: **{affected_path_count}**."
    )
    lines.append("")
    warnings = _markdown_warning_lines(groups)
    lines.extend(warnings or ("(none)",))
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


def _run_warning_groups(
    runs: tuple[BenchmarkRunResult, ...],
) -> tuple[tuple[BenchmarkRunResult, _WarningGroup], ...]:
    return tuple(
        (run, group) for run in runs for group in _group_warnings(run.warnings)
    )


def _group_warnings(
    warnings: tuple[CompletenessWarning, ...],
) -> tuple[_WarningGroup, ...]:
    grouped: dict[
        tuple[str, str, str],
        list[CompletenessWarning],
    ] = {}
    for warning in warnings:
        key = (warning.code, warning.severity, warning.message)
        grouped.setdefault(key, []).append(warning)
    return tuple(
        _WarningGroup(
            code=code,
            severity=severity,
            message=message,
            record_count=len(records),
            paths=tuple(
                sorted(
                    {warning.path for warning in records if warning.path is not None}
                )
            ),
            related_paths=tuple(
                sorted({path for warning in records for path in warning.related_paths})
            ),
            pathless_count=sum(warning.path is None for warning in records),
        )
        for (code, severity, message), records in sorted(grouped.items())
    )


def _warning_counts(
    groups: tuple[tuple[BenchmarkRunResult, _WarningGroup], ...],
) -> tuple[int, int]:
    return len(groups), sum(len(group.paths) for _, group in groups)


def _text_warning_lines(
    groups: tuple[tuple[BenchmarkRunResult, _WarningGroup], ...],
) -> tuple[str, ...]:
    lines: list[str] = []
    for run, group in groups:
        lines.extend(
            (
                f"  {run.task_id} [{run.mode.value}] repeat {run.repetition}:",
                f"    {group.code} [{group.severity}]: "
                f"{group.record_count} {_plural(group.record_count, 'record')}; "
                f"{len(group.paths)} {_plural(len(group.paths), 'affected path')}",
            )
        )
        lines.extend(
            textwrap.wrap(
                _visible_inline(group.message),
                width=_REPORT_WIDTH,
                initial_indent="      Message: ",
                subsequent_indent="        ",
            )
        )
        if group.paths:
            lines.append(f"      Affected paths ({len(group.paths)}):")
            lines.extend(f"        - {path}" for path in group.paths)
        if group.related_paths:
            lines.append(f"      Related paths ({len(group.related_paths)}):")
            lines.extend(f"        - {path}" for path in group.related_paths)
        if group.pathless_count:
            lines.append(f"      Pathless records: {group.pathless_count}")
    return tuple(lines)


def _markdown_warning_lines(
    groups: tuple[tuple[BenchmarkRunResult, _WarningGroup], ...],
) -> tuple[str, ...]:
    lines: list[str] = []
    for run, group in groups:
        lines.append(
            f"- {_markdown_code_span(run.task_id)} [{run.mode.value}] "
            f"repeat {run.repetition}: {_markdown_code_span(group.code)} "
            f"({group.severity}); {group.record_count} "
            f"{_plural(group.record_count, 'record')}; {len(group.paths)} "
            f"{_plural(len(group.paths), 'affected path')}"
        )
        lines.extend(
            textwrap.wrap(
                _escape_markdown_inline(group.message),
                width=_REPORT_WIDTH,
                initial_indent="  - Message: ",
                subsequent_indent="    ",
            )
        )
        if group.paths:
            lines.append(f"  - Affected paths ({len(group.paths)}):")
            lines.extend(f"    - {_markdown_code_span(path)}" for path in group.paths)
        if group.related_paths:
            lines.append(f"  - Related paths ({len(group.related_paths)}):")
            lines.extend(
                f"    - {_markdown_code_span(path)}" for path in group.related_paths
            )
        if group.pathless_count:
            lines.append(f"  - Pathless records: {group.pathless_count}")
    return tuple(lines)


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
                lines.append(f"{label}: {name} budget {limit.actual}>{limit.limit}")
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


def _expectation_outcome(run: BenchmarkRunResult) -> str:
    if run.status != "complete":
        return "unavailable (run did not complete)"
    return "passed" if run.expectations.passed else "failed"


def _range(value: BenchmarkIntegerRange | None) -> str:
    if value is None:
        return "n/a"
    return (
        str(value.minimum)
        if value.minimum == value.maximum
        else f"{value.minimum}-{value.maximum}"
    )


def _plural(count: int, singular: str) -> str:
    return singular if count == 1 else f"{singular}s"


def _markdown_code_span(value: str) -> str:
    visible = _visible_inline(value)
    longest_run = max(
        (len(match.group()) for match in _BACKTICK_RUN.finditer(visible)), default=0
    )
    delimiter = "`" * (longest_run + 1)
    padded = visible
    if visible.startswith(("`", " ")) or visible.endswith(("`", " ")):
        padded = f" {visible} "
    return f"{delimiter}{padded}{delimiter}"


def _escape_markdown_inline(value: str) -> str:
    return _MARKDOWN_PUNCTUATION.sub(r"\\\1", _visible_inline(value))


def _visible_inline(value: str) -> str:
    return "".join(
        _visible_control(character)
        if ord(character) < 32 or ord(character) == 127
        else character
        for character in value
    )


def _visible_control(character: str) -> str:
    escapes = {"\t": r"\t", "\n": r"\n", "\r": r"\r"}
    return escapes.get(character, f"\\u{ord(character):04x}")


__all__ = [
    "render_benchmark_json",
    "render_benchmark_markdown",
    "render_benchmark_text",
]
