"""Pure deterministic renderers for canonical context packages."""

from __future__ import annotations

import json
import re

from contextforge.context.package import ContextBlock, ContextPackage
from contextforge.context.tree import render_project_tree

MAX_JSON_PACKAGE_BYTES = 10_000_000

_BACKTICK_RUN = re.compile(r"`+")
_MARKDOWN_PUNCTUATION = re.compile(r"([\\`*_{}\[\]<>()#+\-.!|>])")


class ContextRenderError(ValueError):
    """Raised when a canonical package cannot be rendered safely."""


def render_context_package_markdown(package: ContextPackage) -> str:
    """Render a package as deterministic, source-safe Markdown.

    The renderer consumes only the canonical package. It performs no selection,
    repository traversal, or file reading.
    """

    if not isinstance(package, ContextPackage):
        raise ContextRenderError("expected a ContextPackage")

    statistics = package.statistics
    lines = [
        f"# {_escape_markdown_inline(package.title)}",
        "",
        f"Task: {_escape_markdown_inline(package.task_description)}",
        "",
        f"Schema version: {package.schema_version}",
    ]

    if package.tree is not None:
        lines.extend(
            (
                "",
                "## Project tree",
                "",
                _render_fenced_block(render_project_tree(package.tree), info="text"),
            )
        )

    languages = _render_language_counts(statistics.languages)
    lines.extend(
        (
            "",
            "## Statistics",
            "",
            f"- Selectable files: {statistics.tree_file_count}",
            f"- Selectable directories: {statistics.tree_directory_count}",
            f"- Selected files: {statistics.selected_file_count}",
            f"- Ranged files: {statistics.ranged_file_count}",
            f"- Selected source bytes: {statistics.selected_source_bytes}",
            f"- Included content bytes: {statistics.included_content_bytes}",
            f"- Included characters: {statistics.included_character_count}",
            f"- Included lines: {statistics.included_line_count}",
            f"- Languages: {languages}",
            "",
            "## Files",
        )
    )

    if not package.files:
        lines.extend(("", "_(none)_"))
    for context_file in package.files:
        included_lines = (
            "all"
            if context_file.selection == "full"
            else ", ".join(
                f"{block.start_line}-{block.end_line}" for block in context_file.blocks
            )
        )
        language = (
            "unknown"
            if context_file.language is None
            else _markdown_code_span(_visible_inline(context_file.language))
        )
        lines.extend(
            (
                "",
                f"### {_markdown_code_span(_visible_inline(context_file.path))}",
                "",
                f"- Language: {language}",
                f"- Source bytes: {context_file.source_size_bytes}",
                f"- Source SHA-256: {context_file.source_sha256}",
                f"- Source lines: {context_file.source_line_count}",
                f"- Included lines: {included_lines}",
            )
        )
        for block in context_file.blocks:
            if context_file.selection == "ranges":
                lines.extend(
                    (
                        "",
                        f"#### Lines {block.start_line}-{block.end_line}",
                    )
                )
            lines.extend(("", _render_source_block(block)))

    rendered = "\n".join(lines).rstrip("\n") + "\n"
    try:
        rendered.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ContextRenderError("package contains invalid Unicode") from exc
    return rendered


def render_context_package_json(
    package: ContextPackage,
    *,
    max_size_bytes: int = MAX_JSON_PACKAGE_BYTES,
) -> str:
    """Serialize a package as canonical schema-versioned JSON."""

    if not isinstance(package, ContextPackage):
        raise ContextRenderError("expected a ContextPackage")
    if type(max_size_bytes) is not int or max_size_bytes <= 0:
        raise ContextRenderError("max_size_bytes must be a positive integer")
    try:
        rendered = (
            json.dumps(
                package.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            + "\n"
        )
        rendered_size = len(rendered.encode("utf-8"))
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ContextRenderError("package cannot be serialized as UTF-8 JSON") from exc
    if rendered_size > max_size_bytes:
        raise ContextRenderError(
            f"rendered JSON has {rendered_size} bytes; limit is {max_size_bytes}"
        )
    return rendered


def _render_source_block(block: ContextBlock) -> str:
    return _render_fenced_block(block.text)


def _render_fenced_block(text: str, *, info: str | None = None) -> str:
    longest_run = max(
        (len(match.group()) for match in _BACKTICK_RUN.finditer(text)), default=0
    )
    fence = "`" * max(3, longest_run + 1)
    opening = fence if info is None else f"{fence}{info}"
    separator = "" if text.endswith("\n") else "\n"
    return f"{opening}\n{text}{separator}{fence}"


def _markdown_code_span(value: str) -> str:
    longest_run = max(
        (len(match.group()) for match in _BACKTICK_RUN.finditer(value)), default=0
    )
    delimiter = "`" * (longest_run + 1)
    padded = value
    if value.startswith(("`", " ")) or value.endswith(("`", " ")):
        padded = f" {value} "
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


def _render_language_counts(languages: dict[str, int]) -> str:
    if not languages:
        return "none"
    return ", ".join(
        f"{_markdown_code_span(_visible_inline(language))}: {count}"
        for language, count in languages.items()
    )
