import json

import pytest

from contextforge.application import canonical_json
from contextforge.discovery import (
    CompletenessWarning,
    DiscoveryBudgetUsage,
    DiscoveryCandidate,
    DiscoveryLineRange,
    DiscoveryMode,
    DiscoveryRenderError,
    DiscoveryResultFormat,
    FinalContextSelection,
    SelectionReason,
    render_context_suggestion,
)


def _selection() -> FinalContextSelection:
    return FinalContextSelection(
        task="Review output rendering",
        mode=DiscoveryMode.HYBRID,
        source_snapshot_digest="a" * 64,
        selected=(
            DiscoveryCandidate(
                candidate_id="candidate:app",
                kind="line_ranges",
                path="src/app.py",
                ranges=(DiscoveryLineRange(start_line=2, end_line=4),),
                reason=SelectionReason(
                    summary="Handles *formatted* output.",
                    discovery_source="model-tool:add_to_context",
                    evidence=("Defines render_output",),
                ),
                confidence=0.75,
                source_sha256="b" * 64,
                model_selected=True,
            ),
        ),
        summary="Selected the output implementation.",
        unknowns=("Terminal color support is unknown.",),
        completeness_warnings=(
            CompletenessWarning(
                code="related-test-missing",
                message="No related test was selected.",
            ),
        ),
        confidence=0.625,
        budget_usage=DiscoveryBudgetUsage(context_bytes=123, context_files=1),
        run_id="run-output",
    )


def test_all_formats_render_the_same_validated_selection() -> None:
    selection = _selection()

    rendered = {
        output_format: render_context_suggestion(
            selection, output_format=output_format, explain=True
        )
        for output_format in DiscoveryResultFormat
    }

    assert rendered == {
        output_format: render_context_suggestion(
            selection, output_format=output_format, explain=True
        )
        for output_format in DiscoveryResultFormat
    }
    assert (
        "src/app.py (2-4, 75% confidence)\n"
        "    reason: Handles *formatted* output."
        in rendered[DiscoveryResultFormat.text]
    )
    assert "Exact confidence: 0.625" in rendered[DiscoveryResultFormat.text]
    assert "candidate:app" not in rendered[DiscoveryResultFormat.text]
    assert "### 1. `src/app.py`" in rendered[DiscoveryResultFormat.markdown]
    assert r"Handles \*formatted\* output\." in rendered[DiscoveryResultFormat.markdown]
    assert "candidate:app" not in rendered[DiscoveryResultFormat.markdown]
    assert "a" * 64 not in rendered[DiscoveryResultFormat.markdown]
    assert json.loads(rendered[DiscoveryResultFormat.json]) == selection.model_dump(
        mode="json"
    )


def test_markdown_report_has_an_exact_stable_concise_shape() -> None:
    rendered = render_context_suggestion(
        _selection(), output_format=DiscoveryResultFormat.markdown
    )

    assert rendered == (
        "# ContextForge context suggestion\n"
        "\n"
        "## Task\n"
        "\n"
        "Review output rendering\n"
        "\n"
        "## Discovery Summary\n"
        "\n"
        "Selected the output implementation\\.\n"
        "\n"
        "- Discovery mode: `hybrid`\n"
        "- Confidence: 0.625\n"
        "- Provenance: model\\-guided selection\n"
        "\n"
        "## Selected Files\n"
        "\n"
        "### 1. `src/app.py`\n"
        "\n"
        "- Lines: 2-4\n"
        "- Confidence: 0.750\n"
        r"- Reason: Handles \*formatted\* output\." "\n"
        "- Provenance: `model-tool:add_to_context`\n"
        "\n"
        "## Deterministic Completeness Additions\n"
        "\n"
        "_(none)_\n"
        "\n"
        "## Warnings\n"
        "\n"
        "### Warnings\n"
        "\n"
        "- `related-test-missing`\n"
        "\n"
        "### Unknowns\n"
        "\n"
        "- Terminal color support is unknown\\.\n"
        "\n"
        "## Counters\n"
        "\n"
        "- Context: 1 file, 123 bytes; read 0 files and 0 source bytes; "
        "0 tool-result bytes\n"
        "- Provider: 0 model calls, 0 HTTP calls, 0 discovery steps\n"
    )


def test_markdown_explanation_and_completeness_groups_are_safe() -> None:
    base = _selection()
    candidate = DiscoveryCandidate.model_validate(
        base.selected[0].model_dump()
        | {"added_by_completeness": True, "model_selected": False}
    )
    selection = FinalContextSelection.model_validate(
        base.model_dump()
        | {
            "task": "Review *output*\n# without a heading",
            "selected": (candidate,),
            "completeness_warnings": (
                *base.completeness_warnings,
                CompletenessWarning(
                    code="index-note",
                    message="Index coverage is advisory.",
                    severity="info",
                ),
            ),
        }
    )

    rendered = render_context_suggestion(
        selection, output_format=DiscoveryResultFormat.markdown, explain=True
    )

    assert "Review \\*output\\*\\\\n\\# without a heading" in rendered
    assert (
        "## Deterministic Completeness Additions\n\n"
        "- `src/app.py`: Handles \\*formatted\\* output\\."
    ) in rendered
    assert "### Information\n\n- `index-note`" in rendered
    assert "  - Reason: Index coverage is advisory\\." in rendered
    assert "Warning confidence (not result confidence): unknown" in rendered
    assert "## Detailed Explanation" in rendered
    assert "- Evidence: Defines render\\_output" in rendered


def test_markdown_preserves_canonical_selection_rank_order() -> None:
    base = _selection()
    later = DiscoveryCandidate(
        candidate_id="candidate:z",
        kind="full_file",
        path="README.md",
        reason=SelectionReason(
            summary="Provides supporting documentation.",
            discovery_source="model-tool:add_to_context",
        ),
        source_sha256="c" * 64,
        model_selected=True,
    )
    selection = FinalContextSelection.model_validate(
        base.model_dump() | {"selected": (*base.selected, later)}
    )

    rendered = render_context_suggestion(
        selection, output_format=DiscoveryResultFormat.markdown
    )

    assert rendered.index("### 1. `src/app.py`") < rendered.index(
        "### 2. `README.md`"
    )


def test_json_rendering_is_byte_compatible_with_existing_output() -> None:
    selection = _selection()

    rendered = render_context_suggestion(
        selection, output_format=DiscoveryResultFormat.json
    )

    assert rendered == canonical_json(selection.model_dump(mode="json"))


def test_duplicate_warnings_are_grouped_with_sorted_deduplicated_paths() -> None:
    base = _selection()
    warnings = (
        CompletenessWarning(
            code="unreadable-source",
            message="Source could not be read; check file permissions.",
            path="src/z.py",
            related_paths=("src/a.py", "src/shared.py"),
            confidence=0.9,
        ),
        CompletenessWarning(
            code="unreadable-source",
            message="Source could not be read; check file permissions.",
            path="src/a.py",
            related_paths=("src/b.py", "src/shared.py"),
            confidence=0.9,
        ),
    )
    selection = FinalContextSelection.model_validate(
        base.model_dump() | {"completeness_warnings": warnings}
    )

    concise = render_context_suggestion(selection)
    explained = render_context_suggestion(selection, explain=True)
    json_output = json.loads(
        render_context_suggestion(selection, output_format=DiscoveryResultFormat.json)
    )

    assert concise.count("unreadable-source (4 affected files)") == 1
    assert "Source could not be read" not in concise
    assert explained.count("Source could not be read") == 1
    assert explained.index("src/a.py") < explained.index("src/b.py")
    assert explained.index("src/b.py") < explained.index("src/shared.py")
    assert explained.index("src/shared.py") < explained.index("src/z.py")
    assert len(json_output["completeness_warnings"]) == 2


def test_warning_groups_keep_mixed_severities_and_reasons_separate() -> None:
    base = _selection()
    selection = FinalContextSelection.model_validate(
        base.model_dump()
        | {
            "completeness_warnings": (
                CompletenessWarning(
                    code="stale-source",
                    message="Refresh the source index.",
                    severity="warning",
                    path="src/app.py",
                ),
                CompletenessWarning(
                    code="stale-source",
                    message="Refresh the source index.",
                    severity="info",
                    path="src/info.py",
                ),
                CompletenessWarning(
                    code="stale-source",
                    message="Re-read the source before continuing.",
                    severity="warning",
                    path="src/other.py",
                ),
            )
        }
    )

    concise = render_context_suggestion(selection)
    rendered = render_context_suggestion(selection, explain=True)

    assert concise.count("stale-source (1 affected file)") == 3
    assert "  Warnings:" in rendered
    assert "  Information:" in rendered
    assert rendered.count("Refresh the source index.") == 2
    assert rendered.count("Re-read the source before continuing.") == 1


@pytest.mark.parametrize(
    "output_format", (DiscoveryResultFormat.text, DiscoveryResultFormat.markdown)
)
def test_warning_rendering_order_is_stable(
    output_format: DiscoveryResultFormat,
) -> None:
    base = _selection()
    warnings = (
        CompletenessWarning(
            code="missing-source", message="Missing.", path="src/z.py"
        ),
        CompletenessWarning(
            code="hash-mismatch", message="Changed.", path="src/b.py"
        ),
        CompletenessWarning(
            code="hash-mismatch", message="Changed.", path="src/a.py"
        ),
    )

    first = FinalContextSelection.model_validate(
        base.model_dump() | {"completeness_warnings": warnings}
    )
    second = FinalContextSelection.model_validate(
        base.model_dump() | {"completeness_warnings": tuple(reversed(warnings))}
    )

    assert render_context_suggestion(
        first, output_format=output_format, explain=True
    ) == render_context_suggestion(second, output_format=output_format, explain=True)


def test_empty_warnings_render_a_stable_empty_summary() -> None:
    base = _selection()
    selection = FinalContextSelection.model_validate(
        base.model_dump()
        | {"completeness_warnings": (), "unknowns": ()}
    )

    text = render_context_suggestion(selection)
    markdown = render_context_suggestion(
        selection, output_format=DiscoveryResultFormat.markdown
    )

    assert "Warnings:\n  (none; this is not a proof of completeness)" in text
    assert "## Warnings\n\n_(none; this is not a proof of completeness)_" in markdown


def test_renderer_selection_is_explicit_and_rejects_unknown_formats() -> None:
    with pytest.raises(DiscoveryRenderError, match="unsupported"):
        render_context_suggestion(
            _selection(),
            output_format="yaml",  # type: ignore[arg-type]
        )
