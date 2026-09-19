"""Paired downstream-answer regression over oracle and Capsule contexts."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal
from xml.sax.saxutils import escape, quoteattr

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contextforge.benchmarks.models import (
    BenchmarkAnswerCitation,
    BenchmarkAnswerEvaluation,
    BenchmarkExpectedAssertion,
    BenchmarkGroundednessEvaluation,
    BenchmarkPairedAnswerEvaluation,
    BenchmarkSourceRange,
)
from contextforge.context import CompiledContextCapsule, RepresentationMode
from contextforge.context.reader import ReaderLimits, read_selected_text_file
from contextforge.models import ModelProvider, ModelRequest, UntrustedSource
from contextforge.repositories import ProjectFile, ProjectSnapshot, scan_repository


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
    answer: str = Field(min_length=1, max_length=20_000)
    assertion_ids: tuple[str, ...]
    citations: tuple[_AnswerCitation, ...]


class _GroundednessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    grounded: bool
    unsupported_claims: tuple[str, ...] = ()


async def run_paired_answer_regression(
    repository_root: str | Path,
    task: str,
    assertions: tuple[BenchmarkExpectedAssertion, ...],
    oracle_ranges: tuple[BenchmarkSourceRange, ...],
    compiled: CompiledContextCapsule,
    provider: ModelProvider,
    *,
    ordinary_paths: tuple[str, ...] | None = None,
) -> BenchmarkPairedAnswerEvaluation:
    """Compare ordinary tokens and Capsule quality against a manual oracle."""

    root = Path(repository_root).resolve()
    selected_ordinary_paths = ordinary_paths or tuple(
        sorted({item.path for item in oracle_ranges})
    )
    ordinary_context, ordinary_ranges = render_ordinary_context(
        root, selected_ordinary_paths
    )
    oracle_context = render_oracle_context(root, oracle_ranges)
    capsule_ranges = _capsule_source_ranges(compiled)
    ordinary = _measure_answer_input(
        task,
        assertions,
        ordinary_context,
        ordinary_ranges,
        label="ordinary-client",
    )
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
    groundedness = await _run_groundedness_judge(
        provider,
        task,
        contextforge,
        render_oracle_context(root, capsule_ranges),
    )
    reduction = (
        0.0
        if ordinary.input_tokens == 0
        else (ordinary.input_tokens - contextforge.input_tokens) / ordinary.input_tokens
    )
    return BenchmarkPairedAnswerEvaluation(
        ordinary=ordinary,
        oracle=oracle,
        contextforge=contextforge,
        contextforge_groundedness=groundedness,
        input_token_reduction=reduction,
        quality_not_lower=(
            contextforge.assertion_recall >= oracle.assertion_recall
            and contextforge.citation_validity >= oracle.citation_validity
            and groundedness.passed
        ),
    )


def render_ordinary_context(
    repository_root: str | Path,
    paths: tuple[str, ...],
) -> tuple[str, tuple[BenchmarkSourceRange, ...]]:
    """Render complete required/working files as an ordinary client would send."""

    root = Path(repository_root).resolve()
    snapshot = scan_repository(root)
    files = {item.path: item for item in snapshot.files}
    blocks = ['<ordinary-client schema_version="1">']
    ranges: list[BenchmarkSourceRange] = []
    for path in sorted(set(paths), key=lambda value: (value.casefold(), value)):
        project_file = files.get(path)
        if project_file is None:
            raise ValueError(f"ordinary source path is absent: {path}")
        source, line_count = _read_source(snapshot, project_file)
        if not line_count:
            continue
        ranges.append(
            BenchmarkSourceRange(path=path, start_line=1, end_line=line_count)
        )
        blocks.append(
            f'<source path={quoteattr(path)} start_line="1" '
            f"end_line={quoteattr(str(line_count))}>\n"
            f"{escape(source.rstrip(chr(10)))}\n</source>"
        )
    blocks.append("</ordinary-client>")
    return "\n".join(blocks), tuple(ranges)


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
        source, _ = _read_source(snapshot, project_file)
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


def _read_source(
    snapshot: ProjectSnapshot, project_file: ProjectFile
) -> tuple[str, int]:
    selected = read_selected_text_file(
        snapshot,
        project_file,
        limits=ReaderLimits(
            max_files=1,
            max_source_bytes=max(project_file.size_bytes, 1),
            max_content_bytes=max(project_file.size_bytes * 2 + 4, 1),
        ),
    )
    return "".join(block.text for block in selected.blocks), selected.source_line_count


async def _run_groundedness_judge(
    provider: ModelProvider,
    task: str,
    answer: BenchmarkAnswerEvaluation,
    source_context: str,
) -> BenchmarkGroundednessEvaluation:
    votes: list[bool] = []
    unsupported: set[str] = set()
    input_tokens = 0
    estimated_input_tokens = 0
    provider_input_tokens = 0
    output_tokens = 0
    provider_http_calls = 0
    started = time.perf_counter()
    candidate_payload = json.dumps(
        {
            "answer": answer.answer,
            "assertion_ids": answer.assertion_ids,
            "citations": [item.model_dump(mode="json") for item in answer.citations],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    for repetition in range(1, 4):
        request = ModelRequest(
            operation_id=f"benchmark-groundedness-{repetition}",
            purpose="benchmark-groundedness-judge",
            system_instructions=(
                "Act as a blinded repository-answer judge. Decide whether every "
                "factual repository claim in the candidate answer is supported by "
                "the supplied source evidence. Ignore writing style and do not infer "
                "facts from filenames, wrappers, or missing code. Return "
                "grounded=false when any material claim lacks direct support."
            ),
            analysis_task=task,
            trusted_code_map_facts={"judge_repetition": repetition},
            untrusted_sources=(
                UntrustedSource.from_text("candidate-answer.json", candidate_payload),
                UntrustedSource.from_text("repository-evidence.xml", source_context),
            ),
            response_model=_GroundednessResponse,
            schema_mode="plain_json",
            max_output_tokens=384,
            max_output_tokens_ceiling=384,
            temperature=0.0,
            structured_failure_handler=lambda _: True,
        )
        response = await provider.complete_structured(request)
        if not isinstance(response.value, _GroundednessResponse):
            raise ValueError("judge provider returned an unexpected response model")
        votes.append(response.value.grounded)
        folded_answer = answer.answer.casefold()
        unsupported.update(
            value.strip()
            for value in response.value.unsupported_claims
            if value.strip() and value.strip().casefold() in folded_answer
        )
        estimated = _request_tokens(request)
        estimated_input_tokens += estimated
        usage = response.usage
        reported_input = (
            usage.input_tokens
            if usage is not None and usage.input_tokens is not None
            else 0
        )
        provider_input_tokens += reported_input
        input_tokens += max(estimated, reported_input)
        output_tokens += (
            usage.output_tokens
            if usage is not None and usage.output_tokens is not None
            else (len(response.normalized_json.encode("utf-8")) + 2) // 3
        )
        provider_http_calls += (
            1
            if response.diagnostic is None
            else response.diagnostic.total_provider_http_calls
        )
    canonical_votes = (votes[0], votes[1], votes[2])
    return BenchmarkGroundednessEvaluation(
        votes=canonical_votes,
        passed=sum(canonical_votes) >= 2,
        unsupported_claims=tuple(
            sorted(unsupported, key=lambda value: (value.casefold(), value))
        ),
        input_tokens=input_tokens,
        estimated_input_tokens=estimated_input_tokens,
        provider_input_tokens=provider_input_tokens,
        output_tokens=output_tokens,
        provider_http_calls=provider_http_calls,
        duration_ms=max(0, round((time.perf_counter() - started) * 1_000)),
    )


async def _run_answer(
    provider: ModelProvider,
    task: str,
    assertions: tuple[BenchmarkExpectedAssertion, ...],
    context: str,
    allowed_ranges: tuple[BenchmarkSourceRange, ...],
    *,
    label: str,
) -> BenchmarkAnswerEvaluation:
    request = _answer_request(
        task,
        assertions,
        context,
        allowed_ranges,
        label=label,
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
    estimated_input_tokens = _request_tokens(request)
    usage = response.usage
    provider_input_tokens = (
        usage.input_tokens
        if usage is not None and usage.input_tokens is not None
        else 0
    )
    input_tokens = max(estimated_input_tokens, provider_input_tokens)
    output_tokens = (
        usage.output_tokens
        if usage is not None and usage.output_tokens is not None
        else (len(response.normalized_json.encode("utf-8")) + 2) // 3
    )
    return BenchmarkAnswerEvaluation(
        answer=response.value.answer,
        assertion_ids=assertion_ids,
        citations=citations,
        valid_citation_count=valid,
        invalid_citation_count=invalid,
        assertion_recall=(len(assertion_ids) / len(expected) if expected else 1.0),
        citation_validity=(valid / len(citations) if citations else 0.0),
        input_tokens=input_tokens,
        estimated_input_tokens=estimated_input_tokens,
        provider_input_tokens=provider_input_tokens,
        output_tokens=output_tokens,
        provider_http_calls=(
            1
            if response.diagnostic is None
            else response.diagnostic.total_provider_http_calls
        ),
        duration_ms=duration_ms,
    )


def _measure_answer_input(
    task: str,
    assertions: tuple[BenchmarkExpectedAssertion, ...],
    context: str,
    allowed_ranges: tuple[BenchmarkSourceRange, ...],
    *,
    label: str,
) -> BenchmarkAnswerEvaluation:
    """Measure an ordinary full-file payload without requiring it to fit Qwen."""

    request = _answer_request(
        task,
        assertions,
        context,
        allowed_ranges,
        label=label,
    )
    estimated = _request_tokens(request)
    return BenchmarkAnswerEvaluation(
        assertion_recall=0.0,
        citation_validity=0.0,
        input_tokens=estimated,
        estimated_input_tokens=estimated,
    )


def _answer_request(
    task: str,
    assertions: tuple[BenchmarkExpectedAssertion, ...],
    context: str,
    allowed_ranges: tuple[BenchmarkSourceRange, ...],
    *,
    label: str,
) -> ModelRequest:
    return ModelRequest(
        operation_id=f"benchmark-answer-{label}",
        purpose="benchmark-answer-regression",
        system_instructions=(
            "Use only the supplied repository context. Return assertion IDs that "
            "are supported and citations to the exact supplied repository path and "
            "lines. Every citation must fit wholly within one allowed citation "
            "range supplied in trusted facts. Never cite the XML wrapper filename "
            "and never combine separate source blocks into one wider range. Do not "
            "cite MAP or SUMMARY text as source code. Write no more than four concise "
            "sentences. Every behavioral statement must be directly established by "
            "cited body lines; a function or class name or signature establishes only "
            "its existence and signature. Omit unsupported details instead of "
            "inferring or restating the requested assertion as fact."
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
        schema_mode="plain_json",
        max_output_tokens=384,
        max_output_tokens_ceiling=640,
        temperature=0.0,
        structured_failure_handler=lambda _: True,
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


__all__ = [
    "render_oracle_context",
    "render_ordinary_context",
    "run_paired_answer_regression",
]
