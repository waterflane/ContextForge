import json
from collections.abc import Callable
from typing import Literal

import pytest

from contextforge.benchmarks import (
    BenchmarkBudgetEvaluation,
    BenchmarkExpectationEvaluation,
    BenchmarkFailure,
    BenchmarkLimitEvaluation,
    BenchmarkMode,
    BenchmarkProviderCounters,
    BenchmarkResult,
    BenchmarkRunResult,
    calculate_benchmark_metrics,
)
from contextforge.benchmarks.renderers import (
    render_benchmark_json,
    render_benchmark_markdown,
    render_benchmark_text,
)
from contextforge.discovery import CompletenessWarning

SOURCE = "a" * 64
CONFIGURATION = "b" * 64


def _limit() -> BenchmarkLimitEvaluation:
    return BenchmarkLimitEvaluation(limit=10, actual=1, passed=True)


def _run(
    *,
    status: Literal["complete", "failed", "cancelled"] = "complete",
    expectation_passed: bool = True,
    confidence: float | None = 0.95,
    warnings: tuple[CompletenessWarning, ...] = (),
) -> BenchmarkRunResult:
    complete = status == "complete"
    return BenchmarkRunResult(
        task_id="renderer",
        repository_path="repository",
        mode=BenchmarkMode.FRESH,
        repetition=1,
        status=status,
        passed=complete and expectation_passed,
        duration_ms=25,
        selected_files=("main.py",) if complete else (),
        files_considered=1 if complete else 0,
        files_read=1 if complete else 0,
        source_snapshot_digest=SOURCE if complete else None,
        effective_configuration_digest=CONFIGURATION,
        provider_id="fake",
        model_id="benchmark-v1",
        provider_counters=BenchmarkProviderCounters(),
        confidence=confidence,
        warnings=warnings,
        provenance="model" if complete else None,
        expectations=BenchmarkExpectationEvaluation(
            required_files=("main.py",),
            matched_required_files=("main.py",)
            if complete and expectation_passed
            else (),
            missing_required_files=()
            if not complete or expectation_passed
            else ("main.py",),
            passed=expectation_passed,
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
                code="discovery_failed",
                error_type="DiscoveryError",
                message="Discovery did not complete.",
            )
        ),
    )


def _result(*runs: BenchmarkRunResult) -> BenchmarkResult:
    return BenchmarkResult(
        manifest_schema_version=1,
        suite_name="renderer",
        runs=runs,
        metrics=calculate_benchmark_metrics(runs),
        passed=all(run.passed for run in runs),
    )


@pytest.mark.parametrize(
    ("renderer", "expected"),
    (
        (
            render_benchmark_text,
            "Benchmark expectations: failed; Discovery confidence: 95.0%",
        ),
        (
            render_benchmark_markdown,
            "| complete | failed | 95.0% |",
        ),
    ),
)
def test_high_discovery_confidence_is_separate_from_failed_expectations(
    renderer: Callable[[BenchmarkResult], str], expected: str
) -> None:
    report = renderer(_result(_run(expectation_passed=False)))

    assert expected in report


@pytest.mark.parametrize(
    ("renderer", "expected"),
    (
        (
            render_benchmark_text,
            "Benchmark expectations: passed; Discovery confidence: 95.0%",
        ),
        (
            render_benchmark_markdown,
            "| complete | passed | 95.0% |",
        ),
    ),
)
def test_successful_expectations_are_separate_from_discovery_confidence(
    renderer: Callable[[BenchmarkResult], str], expected: str
) -> None:
    report = renderer(_result(_run()))

    assert expected in report


@pytest.mark.parametrize("renderer", (render_benchmark_text, render_benchmark_markdown))
def test_incomplete_run_reports_expectations_as_unavailable(
    renderer: Callable[[BenchmarkResult], str],
) -> None:
    report = renderer(_result(_run(status="failed", confidence=None)))

    assert "unavailable (run did not complete)" in report
    assert "Discovery confidence" in report
    assert "n/a" in report


def test_identical_warnings_group_paths_in_deterministic_order() -> None:
    warnings = (
        CompletenessWarning(
            code="source-limited",
            severity="warning",
            message="Source could not be inspected.",
            path="z.py",
        ),
        CompletenessWarning(
            code="source-limited",
            severity="warning",
            message="Source could not be inspected.",
            path="a.py",
        ),
    )
    result = _result(_run(warnings=warnings))

    text = render_benchmark_text(result)
    markdown = render_benchmark_markdown(result)

    assert text.count("source-limited [warning]") == 1
    assert markdown.count("`source-limited` (warning)") == 1
    assert "2 records; 2 affected paths" in text
    assert "2 records; 2 affected paths" in markdown
    assert text.index("- a.py") < text.index("- z.py")
    assert markdown.index("`a.py`") < markdown.index("`z.py`")


def test_distinct_warning_messages_and_severities_remain_separate() -> None:
    warnings = (
        CompletenessWarning(
            code="source-limited",
            message="First reason.",
            path="a.py",
        ),
        CompletenessWarning(
            code="source-limited",
            message="Second reason.",
            path="b.py",
        ),
        CompletenessWarning(
            code="source-limited",
            severity="info",
            message="First reason.",
            path="c.py",
        ),
    )

    for report in (
        render_benchmark_text(_result(_run(warnings=warnings))),
        render_benchmark_markdown(_result(_run(warnings=warnings))),
    ):
        assert "Grouped warnings: 3" in report.replace("**", "")
        assert report.count("First reason") == 2
        assert report.count("Second reason") == 1


def test_pathless_warnings_and_related_paths_remain_visible() -> None:
    warnings = (
        CompletenessWarning(
            code="index-context",
            message="Index context is incomplete.",
            related_paths=("src/a.py", "src/z.py"),
        ),
    )

    for report in (
        render_benchmark_text(_result(_run(warnings=warnings))),
        render_benchmark_markdown(_result(_run(warnings=warnings))),
    ):
        assert "0 affected paths" in report
        assert "Pathless records: 1" in report
        assert "Related paths (2):" in report
        assert report.index("src/a.py") < report.index("src/z.py")


def test_json_warning_records_are_byte_for_byte_unchanged_by_human_rendering() -> None:
    warnings = (
        CompletenessWarning(
            code="source-limited",
            message="Source could not be inspected.",
            path="a.py",
            related_paths=("shared.py",),
            confidence=0.8,
        ),
        CompletenessWarning(
            code="source-limited",
            message="Source could not be inspected.",
            path="b.py",
            related_paths=("shared.py",),
            confidence=0.6,
        ),
    )
    result = _result(_run(warnings=warnings))
    expected = (
        json.dumps(
            result.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )

    before = render_benchmark_json(result)
    text = render_benchmark_text(result)
    markdown = render_benchmark_markdown(result)

    assert "Grouped warnings: 1" in text
    assert "Grouped warnings: **1**" in markdown
    assert before == expected
    assert render_benchmark_json(result) == before
    assert result.runs[0].confidence == 0.95
    assert json.loads(before)["runs"][0]["confidence"] == 0.95
    assert json.loads(before)["runs"][0]["warnings"] == [
        warning.model_dump(mode="json") for warning in warnings
    ]


def test_text_and_markdown_report_equivalent_warning_and_path_counts() -> None:
    warnings = (
        CompletenessWarning(code="same", message="Repeated.", path="a.py"),
        CompletenessWarning(code="same", message="Repeated.", path="b.py"),
        CompletenessWarning(code="other", message="Separate."),
    )
    result = _result(_run(warnings=warnings))

    text = render_benchmark_text(result)
    markdown = render_benchmark_markdown(result)

    assert "Grouped warnings: 2; affected paths: 2" in text
    assert "Grouped warnings: **2**; affected paths: **2**." in markdown


@pytest.mark.parametrize("renderer", (render_benchmark_text, render_benchmark_markdown))
def test_many_affected_paths_use_readable_nested_lines(
    renderer: Callable[[BenchmarkResult], str],
) -> None:
    paths = tuple(f"src/file-{index:02}.py" for index in range(30))
    warnings = tuple(
        CompletenessWarning(code="many-files", message="Repeated.", path=path)
        for path in reversed(paths)
    )

    report = renderer(_result(_run(warnings=warnings)))
    warning_section = report.split("Warnings", maxsplit=1)[1].split(
        "Failed expectations", maxsplit=1
    )[0]

    assert "30 affected paths" in warning_section
    assert all(warning_section.count(path) == 1 for path in paths)
    positions = [warning_section.index(path) for path in paths]
    assert positions == sorted(positions)
