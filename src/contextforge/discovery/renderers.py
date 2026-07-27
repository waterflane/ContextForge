"""Pure deterministic renderers for validated context suggestions."""

from __future__ import annotations

import json
import re
from enum import StrEnum

from contextforge.discovery.models import FinalContextSelection

_BACKTICK_RUN = re.compile(r"`+")
_MARKDOWN_PUNCTUATION = re.compile(r"([\\`*_{}\[\]<>()#+\-.!|>])")


class DiscoveryResultFormat(StrEnum):
    """Supported representations of a context discovery result."""

    text = "text"
    markdown = "markdown"
    json = "json"


class DiscoveryRenderError(ValueError):
    """Raised when a context discovery result cannot be rendered."""


def render_context_suggestion(
    selection: FinalContextSelection,
    *,
    output_format: DiscoveryResultFormat = DiscoveryResultFormat.text,
    explain: bool = False,
) -> str:
    """Render one validated selection without changing or rediscovering it."""

    if not isinstance(selection, FinalContextSelection):
        raise DiscoveryRenderError("expected a FinalContextSelection")
    if output_format is DiscoveryResultFormat.text:
        return _render_text(selection, explain=explain)
    if output_format is DiscoveryResultFormat.markdown:
        return _render_markdown(selection, explain=explain)
    if output_format is DiscoveryResultFormat.json:
        return _render_json(selection)
    raise DiscoveryRenderError(f"unsupported discovery result format: {output_format}")


def _render_text(selection: FinalContextSelection, *, explain: bool) -> str:
    lines = [
        "ContextForge context suggestion",
        f"Discovery mode: {selection.mode.value}",
        f"Estimated size: {selection.budget_usage.context_bytes} bytes",
        f"Selected files: {selection.budget_usage.context_files}",
        f"Confidence: {selection.confidence:.3f}",
        "Selections:",
    ]
    for item in selection.selected:
        path = item.path or f"[{item.kind}]"
        ranges = (
            "all lines"
            if not item.ranges
            else ", ".join(
                f"{value.start_line}-{value.end_line}" for value in item.ranges
            )
        )
        confidence = "unknown" if item.confidence is None else f"{item.confidence:.3f}"
        lines.extend(
            (
                f"  {path} | {ranges} | confidence {confidence}",
                f"    reason: {item.reason.summary}",
            )
        )
        if explain:
            lines.append(f"    source: {item.reason.discovery_source}")
            lines.extend(f"    evidence: {value}" for value in item.reason.evidence)
    lines.append("Warnings:")
    if selection.completeness_warnings:
        lines.extend(
            f"  {item.code}: {item.message}" for item in selection.completeness_warnings
        )
    else:
        lines.append("  (none; this is not a proof of completeness)")
    if selection.unknowns:
        lines.append("Unknowns:")
        lines.extend(f"  {item}" for item in selection.unknowns)
    return "\n".join(lines) + "\n"


def _render_markdown(selection: FinalContextSelection, *, explain: bool) -> str:
    lines = [
        "# ContextForge context suggestion",
        "",
        f"- Discovery mode: {_markdown_code_span(selection.mode.value)}",
        f"- Estimated size: {selection.budget_usage.context_bytes} bytes",
        f"- Selected files: {selection.budget_usage.context_files}",
        f"- Confidence: {selection.confidence:.3f}",
        "",
        "## Selections",
    ]
    for item in selection.selected:
        path = item.path or f"[{item.kind}]"
        ranges = (
            "all lines"
            if not item.ranges
            else ", ".join(
                f"{value.start_line}-{value.end_line}" for value in item.ranges
            )
        )
        confidence = "unknown" if item.confidence is None else f"{item.confidence:.3f}"
        lines.extend(
            (
                "",
                f"### {_markdown_code_span(path)}",
                "",
                f"- Lines: {ranges}",
                f"- Confidence: {confidence}",
                f"- Reason: {_escape_markdown_inline(item.reason.summary)}",
            )
        )
        if explain:
            lines.append(
                f"- Source: {_markdown_code_span(item.reason.discovery_source)}"
            )
            lines.extend(
                f"- Evidence: {_escape_markdown_inline(value)}"
                for value in item.reason.evidence
            )
    lines.extend(("", "## Warnings", ""))
    if selection.completeness_warnings:
        lines.extend(
            f"- {_markdown_code_span(item.code)}: "
            f"{_escape_markdown_inline(item.message)}"
            for item in selection.completeness_warnings
        )
    else:
        lines.append("_(none; this is not a proof of completeness)_")
    if selection.unknowns:
        lines.extend(("", "## Unknowns", ""))
        lines.extend(
            f"- {_escape_markdown_inline(item)}" for item in selection.unknowns
        )
    return "\n".join(lines) + "\n"


def _render_json(selection: FinalContextSelection) -> str:
    return (
        json.dumps(
            selection.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )


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
    "DiscoveryRenderError",
    "DiscoveryResultFormat",
    "render_context_suggestion",
]
