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
        "- `related-test-missing`: No related test was selected\\.\n"
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
    assert (
        "### Information\n\n- `index-note`: Index coverage is advisory\\."
        in rendered
    )
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


def test_renderer_selection_is_explicit_and_rejects_unknown_formats() -> None:
    with pytest.raises(DiscoveryRenderError, match="unsupported"):
        render_context_suggestion(
            _selection(),
            output_format="yaml",  # type: ignore[arg-type]
        )
