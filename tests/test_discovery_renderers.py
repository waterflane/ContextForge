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
    assert "### `src/app.py`" in rendered[DiscoveryResultFormat.markdown]
    assert r"Handles \*formatted\* output\." in rendered[DiscoveryResultFormat.markdown]
    assert json.loads(rendered[DiscoveryResultFormat.json]) == selection.model_dump(
        mode="json"
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
