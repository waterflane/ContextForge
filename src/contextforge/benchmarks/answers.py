"""Paired downstream-answer regression over oracle and Capsule contexts."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape, quoteattr

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contextforge.benchmarks.models import (
    BenchmarkAnswerCitation,
    BenchmarkAnswerEvaluation,
    BenchmarkExpectedAssertion,
    BenchmarkPairedAnswerEvaluation,
    BenchmarkSourceRange,
)
from contextforge.context import CompiledContextCapsule, RepresentationMode
from contextforge.context.reader import ReaderLimits, read_selected_text_file
from contextforge.models import ModelProvider, ModelRequest, UntrustedSource
from contextforge.repositories import scan_repository


class _AnswerCitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assertion_id: str
    path: str
    start_line: int = Field(ge=1, strict=True)
    end_line: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def validate_order(self) -> _AnswerCitation:
        if self.end_line < self.start_line:
            raise ValueError("citation end must not precede its start")
        return self


class _AnswerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    assertion_ids: tuple[str, ...]
    citations: tuple[_AnswerCitation, ...]


async def run_paired_answer_regression(
    repository_root: str | Path,
    task: str,
    assertions: tuple[BenchmarkExpectedAssertion, ...],
    oracle_ranges: tuple[BenchmarkSourceRange, ...],
    compiled: CompiledContextCapsule,
    provider: ModelProvider,
) -> BenchmarkPairedAnswerEvaluation:
    """Run the same closed-schema answer check against oracle and Capsule inputs."""

    root = Path(repository_root).resolve()
    oracle_context = render_oracle_context(root, oracle_ranges)
    capsule_ranges = _capsule_source_ranges(compiled)
    oracle = await _run_answer(
        provider,
        task,
        assertions,
        oracle_context,
        oracle_ranges,
        label="manual-oracle",
    )
    contextforge = await _run_answer(
        provider,
        task,
        assertions,
        compiled.prompt,
        capsule_ranges,
        label="contextforge-capsule",
    )
    reduction = (
        0.0
        if oracle.input_tokens == 0
        else (oracle.input_tokens - contextforge.input_tokens) / oracle.input_tokens
    )
    return BenchmarkPairedAnswerEvaluation(
        oracle=oracle,
        contextforge=contextforge,
        input_token_reduction=reduction,
        quality_not_lower=(
            contextforge.assertion_recall >= oracle.assertion_recall
            and contextforge.citation_validity >= oracle.citation_validity
        ),
    )


def render_oracle_context(
    repository_root: str | Path,
    ranges: tuple[BenchmarkSourceRange, ...],
) -> str:
    """Render source blocks with their real path and inclusive line identity."""

    root = Path(repository_root).resolve()
    snapshot = scan_repository(root)
    files = {item.path: item for item in snapshot.files}
    blocks = ['<manual-oracle schema_version="1">']
    for selected_range in ranges:
        project_file = files.get(selected_range.path)
        if project_file is None:
            raise ValueError(f"oracle source path is absent: {selected_range.path}")
        selected = read_selected_text_file(
            snapshot,
            project_file,
            limits=ReaderLimits(
                max_files=1,
                max_source_bytes=max(project_file.size_bytes, 1),
                max_content_bytes=max(project_file.size_bytes * 2 + 4, 1),
            ),
        )
        source = "".join(block.text for block in selected.blocks)
        lines = source.splitlines()
        if selected_range.end_line > len(lines):
            raise ValueError(f"oracle source range exceeds file: {selected_range.path}")
        content = "\n".join(
            lines[selected_range.start_line - 1 : selected_range.end_line]
        )
        blocks.append(
            f"<source path={quoteattr(selected_range.path)} "
            f"start_line={quoteattr(str(selected_range.start_line))} "
            f"end_line={quoteattr(str(selected_range.end_line))}>\n"
            f"{escape(content)}\n</source>"
        )
    blocks.append("</manual-oracle>")
    return "\n".join(blocks)


async def _run_answer(
    provider: ModelProvider,
    task: str,
    assertions: tuple[BenchmarkExpectedAssertion, ...],
    context: str,
    allowed_ranges: tuple[BenchmarkSourceRange, ...],
    *,
    label: str,
) -> BenchmarkAnswerEvaluation:
    request = ModelRequest(
        operation_id=f"benchmark-answer-{label}",
        purpose="benchmark-answer-regression",
        system_instructions=(
            "Use only the supplied repository context. Return assertion IDs that "
            "are supported and citations to the exact supplied repository path and "
            "lines. Every citation must fit wholly within one allowed citation "
            "range supplied in trusted facts. Never cite the XML wrapper filename "
            "and never combine separate source blocks into one wider range. Do not "
            "cite MAP or SUMMARY text as source code."
        ),
        analysis_task=task,
        trusted_code_map_facts={
            "assertions": [item.model_dump(mode="json") for item in assertions],
            "allowed_citation_ranges": [
                item.model_dump(mode="json") for item in allowed_ranges
            ],
        },
        untrusted_sources=(UntrustedSource.from_text(f"{label}.xml", context),),
        response_model=_AnswerResponse,
        schema_mode="json_schema",
        max_output_tokens=768,
        max_output_tokens_ceiling=768,
        temperature=0.0,
    )
    started = time.perf_counter()
    response = await provider.complete_structured(request)
    duration_ms = max(0, round((time.perf_counter() - started) * 1_000))
    if not isinstance(response.value, _AnswerResponse):
        raise ValueError("answer provider returned an unexpected response model")
    expected = {item.assertion_id for item in assertions}
    assertion_ids = tuple(sorted(set(response.value.assertion_ids) & expected))
    citations = tuple(
        BenchmarkAnswerCitation(
            assertion_id=item.assertion_id,
            path=item.path,
            start_line=item.start_line,
            end_line=item.end_line,
        )
        for item in response.value.citations
    )
    valid = sum(
        item.assertion_id in assertion_ids
        and any(_contains(allowed, item) for allowed in allowed_ranges)
        for item in citations
    )
    invalid = len(citations) - valid
    usage = response.usage
    input_tokens = (
        usage.input_tokens
        if usage is not None and usage.input_tokens is not None
        else _request_tokens(request)
    )
    output_tokens = (
        usage.output_tokens
        if usage is not None and usage.output_tokens is not None
        else (len(response.normalized_json.encode("utf-8")) + 2) // 3
    )
    return BenchmarkAnswerEvaluation(
        assertion_ids=assertion_ids,
        citations=citations,
        valid_citation_count=valid,
        invalid_citation_count=invalid,
        assertion_recall=(len(assertion_ids) / len(expected) if expected else 1.0),
        citation_validity=(valid / len(citations) if citations else 0.0),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
    )


def _capsule_source_ranges(
    compiled: CompiledContextCapsule,
) -> tuple[BenchmarkSourceRange, ...]:
    values: list[BenchmarkSourceRange] = []
    for material in (
        *compiled.capsule.working_set,
        *compiled.capsule.task_context,
    ):
        if material.representation is RepresentationMode.SLICE:
            values.extend(
                BenchmarkSourceRange(
                    path=material.path,
                    start_line=item.start_line,
                    end_line=item.end_line,
                )
                for item in material.ranges
            )
        elif material.representation is RepresentationMode.FULL:
            line_count = len(material.content.splitlines())
            if line_count:
                values.append(
                    BenchmarkSourceRange(
                        path=material.path,
                        start_line=1,
                        end_line=line_count,
                    )
                )
    return tuple(values)


def _contains(allowed: BenchmarkSourceRange, citation: BenchmarkAnswerCitation) -> bool:
    return (
        allowed.path == citation.path
        and allowed.start_line <= citation.start_line
        and citation.end_line <= allowed.end_line
    )


def _request_tokens(request: ModelRequest) -> int:
    return sum(
        (len(message.content.encode("utf-8")) + 2) // 3
        for message in request.messages(include_response_schema=True)
    )


__all__ = ["render_oracle_context", "run_paired_answer_regression"]
