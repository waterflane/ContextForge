"""Compact source projection for models that consume a compiled capsule."""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from typing import Any

from contextforge.context.capsule import CompiledContextCapsule


def render_capsule_source_context(
    compiled: CompiledContextCapsule,
    functions: Sequence[Mapping[str, Any]] = (),
) -> str:
    """Render physical source once; proof metadata remains in the capsule."""
    capsule = compiled.capsule
    blocks = ["<context>", f"<task>{html.escape(capsule.task)}</task>"]
    for material in (*capsule.working_set, *capsule.task_context):
        physical_source = material.representation.value in {"slice", "full"}
        spans = (
            [(span.start_line, span.end_line) for span in material.ranges]
            if material.ranges
            else [(1, len(material.content.splitlines()))]
            if physical_source
            else []
        )
        lines = ",".join(f"{start}-{end}" for start, end in spans)
        blocks.append(
            f'<file path="{html.escape(material.path, quote=True)}" '
            f'representation="{material.representation.value}" lines="{lines}">'
        )
        for function in functions:
            start = function["range"]["start_line"]
            end = function["implementation_end_line"]
            if function["path"] != material.path or not any(
                first <= start and end <= last for first, last in spans
            ):
                continue
            blocks.append(
                f'<function name="{html.escape(function["name"], quote=True)}" '
                f'lines="{start}-{end}">'
            )
            if function.get("purpose"):
                blocks.append(
                    f'<purpose basis="source-observations">'
                    f"{html.escape(function['purpose'])}</purpose>"
                )
            blocks.append("</function>")
        label = "code" if physical_source else "orientation"
        if physical_source:
            numbered: list[str] = []
            current = 1
            remaining = 0
            pending = iter(spans)
            next_span = next(pending, None)
            for line in material.content.splitlines():
                if material.representation.value == "full":
                    numbered.append(f"{current}: {line}")
                    current += 1
                elif remaining:
                    numbered.append(f"{current}: {line}")
                    current += 1
                    remaining -= 1
                elif (
                    next_span
                    and line == f"{material.path}:{next_span[0]}-{next_span[1]}"
                ):
                    current = next_span[0]
                    remaining = next_span[1] - current + 1
                    next_span = next(pending, None)
                elif line.strip():
                    raise ValueError("Unexpected source slice framing")
            if material.representation.value == "slice" and (remaining or next_span):
                raise ValueError("Incomplete source slice framing")
            body = "\n".join(numbered)
        else:
            body = material.content
        blocks.append(f"<{label}>{html.escape(body)}</{label}>")
        blocks.append("</file>")
    if capsule.git_context:
        blocks.append(f"<git>{html.escape(capsule.git_context)}</git>")
    for interpretation in capsule.interpretations:
        blocks.append(f"<interpretation>{html.escape(interpretation)}</interpretation>")
    blocks.append("</context>")
    return "\n".join(blocks)
