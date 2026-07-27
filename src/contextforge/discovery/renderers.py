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
        f"Task: {_visible_inline(selection.task)}",
        f"Discovery mode: {selection.mode.value}",
        f"Confidence: {_percentage(selection.confidence)}",
        f"Provenance: {_provenance_label(selection.provenance)}",
        "Selected files:",
    ]
    for item in selection.selected:
        path = item.path or _selection_type_label(item.kind)
        ranges = (
            "all lines"
            if not item.ranges
            else ", ".join(
                f"{value.start_line}-{value.end_line}" for value in item.ranges
            )
        )
        confidence = (
            "confidence unknown"
            if item.confidence is None
            else f"{_percentage(item.confidence)} confidence"
        )
        lines.extend(
            (
                f"  {_visible_inline(path)} ({ranges}, {confidence})",
                f"    reason: {_visible_inline(item.reason.summary)}",
            )
        )
    lines.append("Warnings:")
    warning_groups = (
        (
            "Warnings",
            tuple(
                item
                for item in selection.completeness_warnings
                if item.severity == "warning"
            ),
        ),
        (
            "Information",
            tuple(
                item
                for item in selection.completeness_warnings
                if item.severity == "info"
            ),
        ),
    )
    for label, warnings in warning_groups:
        if warnings:
            lines.append(f"  {label}:")
            lines.extend(f"    {_visible_inline(item.message)}" for item in warnings)
    if selection.unknowns:
        lines.append("  Unknowns:")
        lines.extend(f"    {_visible_inline(item)}" for item in selection.unknowns)
    if not selection.completeness_warnings and not selection.unknowns:
        lines.append("  (none; this is not a proof of completeness)")
    usage = selection.budget_usage
    lines.append(
        "Performance: "
        f"selected {usage.context_files} {_plural(usage.context_files, 'file')} "
        f"({usage.context_bytes} bytes); "
        f"{usage.steps} {_plural(usage.steps, 'step')}, "
        f"{usage.model_calls} model {_plural(usage.model_calls, 'call')}, "
        f"read {usage.files_read} {_plural(usage.files_read, 'file')} "
        f"({usage.source_bytes} bytes)."
    )
    if explain:
        lines.extend(
            (
                "Technical selection details:",
                f"  Exact confidence: {selection.confidence}",
            )
        )
        for item in selection.selected:
            path = item.path or _selection_type_label(item.kind)
            lines.append(f"  {_visible_inline(path)}:")
            lines.append(f"    Selection type: {_selection_type_label(item.kind)}")
            if item.confidence is not None:
                lines.append(f"    Exact confidence: {item.confidence}")
            lines.append(
                f"    Discovery source: {_visible_inline(item.reason.discovery_source)}"
            )
            if item.source_sha256 is not None:
                lines.append(f"    Verified source SHA-256: {item.source_sha256}")
            lines.extend(
                f"    Evidence: {_visible_inline(value)}"
                for value in item.reason.evidence
            )
    return "\n".join(lines) + "\n"


def _percentage(value: float) -> str:
    return f"{value * 100:.1f}".rstrip("0").rstrip(".") + "%"


def _provenance_label(value: str) -> str:
    return {
        "model": "model-guided selection",
        "deterministic_fallback": "deterministic fallback",
    }[value]


def _selection_type_label(value: str) -> str:
    return {
        "full_file": "full file",
        "line_ranges": "line ranges",
        "codemap": "code map",
        "architecture_note": "repository architecture context",
        "git_diff": "Git diff",
        "related_test": "related test",
    }[value]


def _plural(count: int, singular: str) -> str:
    return singular if count == 1 else f"{singular}s"


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
