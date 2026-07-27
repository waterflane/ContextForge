"""Pure deterministic renderers for validated context suggestions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum

from contextforge.discovery.models import CompletenessWarning, FinalContextSelection

_BACKTICK_RUN = re.compile(r"`+")
_MARKDOWN_PUNCTUATION = re.compile(r"([\\`*_{}\[\]<>()#+\-.!|>])")


class DiscoveryResultFormat(StrEnum):
    """Supported representations of a context discovery result."""

    text = "text"
    markdown = "markdown"
    json = "json"


class DiscoveryRenderError(ValueError):
    """Raised when a context discovery result cannot be rendered."""


@dataclass(frozen=True)
class _WarningGroup:
    code: str
    message: str
    severity: str
    confidence: float | None
    paths: tuple[str, ...]


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
    aggregated = _aggregate_warnings(selection.completeness_warnings)
    warning_groups = (
        (
            "Warnings",
            tuple(item for item in aggregated if item.severity == "warning"),
        ),
        (
            "Information",
            tuple(item for item in aggregated if item.severity == "info"),
        ),
    )
    for label, warnings in warning_groups:
        if warnings:
            lines.append(f"  {label}:")
            for item in warnings:
                lines.append(f"    {_text_warning_summary(item)}")
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
        f"{usage.model_generations} model "
        f"{_plural(usage.model_generations, 'generation')}, "
        f"{usage.repair_generations} repair "
        f"{_plural(usage.repair_generations, 'generation')}, "
        f"{usage.transport_attempts} transport "
        f"{_plural(usage.transport_attempts, 'attempt')}, "
        f"{usage.provider_discovery_calls} provider discovery "
        f"{_plural(usage.provider_discovery_calls, 'call')}, "
        f"{usage.provider_capability_calls} provider capability "
        f"{_plural(usage.provider_capability_calls, 'call')}, "
        f"{usage.total_provider_http_calls} provider HTTP "
        f"{_plural(usage.total_provider_http_calls, 'call')}, "
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
        if aggregated:
            lines.append("  Warning details:")
            for item in aggregated:
                lines.append(f"    {_text_warning_summary(item)}")
                lines.append(f"      Reason: {_visible_inline(item.message)}")
                confidence = (
                    "unknown" if item.confidence is None else str(item.confidence)
                )
                lines.append(
                    "      Warning confidence (not result confidence): "
                    f"{confidence}"
                )
                if item.paths:
                    lines.append("      Affected files:")
                    lines.extend(
                        f"        {_visible_inline(path)}" for path in item.paths
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
        "## Task",
        "",
        _escape_markdown_inline(selection.task),
        "",
        "## Discovery Summary",
        "",
        _escape_markdown_inline(selection.summary),
        "",
        f"- Discovery mode: {_markdown_code_span(selection.mode.value)}",
        f"- Confidence: {selection.confidence:.3f}",
        "- Provenance: "
        f"{_escape_markdown_inline(_provenance_label(selection.provenance))}",
        "",
        "## Selected Files",
    ]
    for rank, item in enumerate(selection.selected, start=1):
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
                f"### {rank}. {_markdown_code_span(path)}",
                "",
                f"- Lines: {ranges}",
                f"- Confidence: {confidence}",
                f"- Reason: {_escape_markdown_inline(item.reason.summary)}",
                f"- Provenance: {_markdown_code_span(item.reason.discovery_source)}",
            )
        )

    additions = tuple(
        item for item in selection.selected if item.added_by_completeness
    )
    lines.extend(("", "## Deterministic Completeness Additions", ""))
    if additions:
        lines.extend(
            f"- {_markdown_code_span(item.path or f'[{item.kind}]')}: "
            f"{_escape_markdown_inline(item.reason.summary)}"
            for item in additions
        )
    else:
        lines.append("_(none)_")

    lines.extend(("", "## Warnings", ""))
    aggregated = _aggregate_warnings(selection.completeness_warnings)
    warning_groups = (
        (
            "Warnings",
            tuple(item for item in aggregated if item.severity == "warning"),
        ),
        (
            "Information",
            tuple(item for item in aggregated if item.severity == "info"),
        ),
    )
    any_warnings = False
    for label, warnings in warning_groups:
        if not warnings:
            continue
        any_warnings = True
        lines.extend((f"### {label}", ""))
        for item in warnings:
            lines.append(f"- {_markdown_warning_summary(item)}")
            if explain:
                lines.append(
                    f"  - Reason: {_escape_markdown_inline(item.message)}"
                )
                confidence = (
                    "unknown" if item.confidence is None else f"{item.confidence:.3f}"
                )
                lines.append(
                    "  - Warning confidence (not result confidence): "
                    f"{confidence}"
                )
                if item.paths:
                    lines.append("  - Affected files:")
                    lines.extend(
                        f"    - {_markdown_code_span(path)}" for path in item.paths
                    )
        lines.append("")
    if selection.unknowns:
        any_warnings = True
        lines.extend(("### Unknowns", ""))
        lines.extend(
            f"- {_escape_markdown_inline(item)}" for item in selection.unknowns
        )
    if not any_warnings:
        lines.append("_(none; this is not a proof of completeness)_")
    if lines[-1] == "":
        lines.pop()

    usage = selection.budget_usage
    lines.extend(
        (
            "",
            "## Counters",
            "",
            f"- Context: {usage.context_files} "
            f"{_plural(usage.context_files, 'file')}, {usage.context_bytes} bytes; "
            f"read {usage.files_read} {_plural(usage.files_read, 'file')} and "
            f"{usage.source_bytes} source bytes; "
            f"{usage.tool_result_bytes} tool-result bytes",
            f"- Provider generations: {usage.model_generations} model, "
            f"{usage.repair_generations} repair; legacy model requests: "
            f"{usage.model_calls}",
            f"- Provider HTTP: {usage.transport_attempts} transport attempts, "
            f"{usage.provider_discovery_calls} discovery calls, "
            f"{usage.provider_capability_calls} capability calls, "
            f"{usage.total_provider_http_calls} total; legacy "
            f"provider_http_calls: {usage.provider_http_calls}; {usage.steps} "
            f"discovery {_plural(usage.steps, 'step')}",
        )
    )
    if explain:
        lines.extend(("", "## Detailed Explanation", ""))
        for rank, item in enumerate(selection.selected, start=1):
            path = item.path or f"[{item.kind}]"
            selection_type = _escape_markdown_inline(
                _selection_type_label(item.kind)
            )
            exact_confidence = (
                item.confidence if item.confidence is not None else "unknown"
            )
            lines.extend(
                (
                    f"### {rank}. {_markdown_code_span(path)}",
                    "",
                    f"- Selection type: {selection_type}",
                    f"- Exact confidence: {exact_confidence}",
                    f"- Manually pinned: {'yes' if item.manually_pinned else 'no'}",
                    f"- Model selected: {'yes' if item.model_selected else 'no'}",
                    "- Added by deterministic completeness review: "
                    f"{'yes' if item.added_by_completeness else 'no'}",
                )
            )
            lines.extend(
                f"- Evidence: {_escape_markdown_inline(value)}"
                for value in item.reason.evidence
            )
            lines.append("")
        if lines[-1] == "":
            lines.pop()
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


def _aggregate_warnings(
    warnings: tuple[CompletenessWarning, ...],
) -> tuple[_WarningGroup, ...]:
    grouped: dict[tuple[str, str, str, float | None], set[str]] = {}
    for warning in warnings:
        key = (
            warning.code,
            warning.severity,
            warning.message,
            warning.confidence,
        )
        grouped.setdefault(key, set()).update(
            path
            for path in (warning.path, *warning.related_paths)
            if path is not None
        )
    return tuple(
        _WarningGroup(
            code=key[0],
            severity=key[1],
            message=key[2],
            confidence=key[3],
            paths=tuple(sorted(paths)),
        )
        for key, paths in sorted(
            grouped.items(),
            key=lambda item: (
                item[0][1],
                item[0][0],
                item[0][2],
                -1.0 if item[0][3] is None else item[0][3],
                tuple(sorted(item[1])),
            ),
        )
    )


def _text_warning_summary(group: _WarningGroup) -> str:
    summary = _visible_inline(group.code)
    if group.paths:
        summary += (
            f" ({len(group.paths)} "
            f"{_plural(len(group.paths), 'affected file')})"
        )
    return summary


def _markdown_warning_summary(group: _WarningGroup) -> str:
    summary = _markdown_code_span(group.code)
    if group.paths:
        summary += (
            f" ({len(group.paths)} "
            f"{_plural(len(group.paths), 'affected file')})"
        )
    return summary


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
