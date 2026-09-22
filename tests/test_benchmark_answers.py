import asyncio
import json
from pathlib import Path

import pytest

from contextforge.application import build_repository_index
from contextforge.benchmarks import (
    BenchmarkAnswerCitation,
    BenchmarkAnswerEvaluation,
    BenchmarkExpectedAssertion,
    BenchmarkSourceRange,
    render_oracle_context,
    render_ordinary_context,
    run_paired_answer_regression,
)
from contextforge.benchmarks.answers import _run_groundedness_judge
from contextforge.context import ContextBudget, compile_context_capsule
from contextforge.intelligence import retrieve_context_candidates
from contextforge.models import FakeModelProvider, ModelRequest, ProviderConfiguration


def test_oracle_and_capsule_answers_keep_real_citation_identity(
    tmp_path: Path,
) -> None:
    source = "def serve(value: str) -> str:\n    return value.upper()\n" + "".join(
        f"# ordinary filler {line}\n" for line in range(1, 41)
    )
    (tmp_path / "service.py").write_text(source, encoding="utf-8", newline="")
    report = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    retrieval = asyncio.run(
        retrieve_context_candidates(tmp_path, "serve", manifest=report.manifest)
    )
    compiled = compile_context_capsule(
        tmp_path,
        "serve",
        retrieval,
        manifest=report.manifest,
        working_files=("service.py",),
        pinned_full_files=("service.py",),
        budget=ContextBudget(
            context_window_tokens=8_192,
            response_tokens=512,
            safety_margin_tokens=256,
        ),
    )
    assertions = (
        BenchmarkExpectedAssertion(
            assertion_id="upper-result",
            description="serve returns the upper-case input",
        ),
    )
    ranges = (BenchmarkSourceRange(path="service.py", start_line=1, end_line=2),)
    seen_allowed_ranges: list[list[object]] = []
    judge_calls = 0

    def respond(request: ModelRequest, call: int) -> str:
        nonlocal judge_calls
        del call
        if request.purpose == "benchmark-groundedness-judge":
            judge_calls += 1
            return json.dumps(
                {
                    "schema_version": 1,
                    "grounded": judge_calls != 1,
                    "unsupported_claims": (
                        ["one disputed claim"] if judge_calls == 1 else []
                    ),
                }
            )
        facts = request.trusted_code_map_facts
        seen_allowed_ranges.append(facts["allowed_citation_ranges"])
        return json.dumps(
            {
                "schema_version": 1,
                "answer": "serve returns the upper-case input.",
                "assertion_ids": ["upper-result"],
                "citations": [
                    {
                        "assertion_id": "upper-result",
                        "path": "service.py",
                        "start_line": 1,
                        "end_line": 2,
                    }
                ],
            }
        )

    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="http://127.0.0.1:1",
            model_id="answer-test",
            retry_limit=0,
            max_json_repair_attempts=0,
        ),
        responder=respond,
    )

    result = asyncio.run(
        run_paired_answer_regression(
            tmp_path,
            "serve",
            assertions,
            ranges,
            compiled,
            provider,
        )
    )

    assert '<source path="service.py" start_line="1" end_line="2">' in (
        render_oracle_context(tmp_path, ranges)
    )
    assert result.oracle.citation_validity == 1.0
    assert result.contextforge.citation_validity == 1.0
    assert result.ordinary is not None
    assert result.contextforge.answer == "serve returns the upper-case input."
    assert result.contextforge_groundedness is not None
    assert result.contextforge_groundedness.votes == (False, True, True)
    assert result.contextforge_groundedness.passed is True
    assert result.quality_not_lower is True
    assert result.ordinary.input_tokens > result.oracle.input_tokens
    assert result.ordinary.provider_http_calls == 0
    assert result.input_token_reduction == pytest.approx(
        (result.ordinary.input_tokens - result.contextforge.input_tokens)
        / result.ordinary.input_tokens
    )
    assert seen_allowed_ranges == [
        [{"path": "service.py", "start_line": 1, "end_line": 2}],
        [{"path": "service.py", "start_line": 1, "end_line": 42}],
    ]


def test_oracle_renderer_rejects_missing_and_out_of_bounds_sources(
    tmp_path: Path,
) -> None:
    (tmp_path / "service.py").write_text("line one\n", encoding="utf-8")

    with pytest.raises(ValueError, match="source path is absent"):
        render_oracle_context(
            tmp_path,
            (BenchmarkSourceRange(path="missing.py", start_line=1, end_line=1),),
        )
    with pytest.raises(ValueError, match="source range exceeds"):
        render_oracle_context(
            tmp_path,
            (BenchmarkSourceRange(path="service.py", start_line=1, end_line=2),),
        )


def test_ordinary_renderer_uses_complete_required_files(tmp_path: Path) -> None:
    (tmp_path / "service.py").write_text(
        "first\nsecond\nthird\n", encoding="utf-8", newline=""
    )

    rendered, ranges = render_ordinary_context(tmp_path, ("service.py",))

    assert ranges == (
        BenchmarkSourceRange(path="service.py", start_line=1, end_line=3),
    )
    assert "first\nsecond\nthird" in rendered


def test_dispose_body_is_required_for_blinded_groundedness(tmp_path: Path) -> None:
    (tmp_path / "lifecycle.py").write_text(
        "def dispose(resource):\n    resource.close()\n",
        encoding="utf-8",
        newline="",
    )
    assertion = BenchmarkExpectedAssertion(
        assertion_id="dispose-closes-resource",
        description="dispose closes the resource",
    )
    answer = BenchmarkAnswerEvaluation(
        answer="dispose closes the resource.",
        assertion_ids=(assertion.assertion_id,),
        citations=(
            BenchmarkAnswerCitation(
                assertion_id=assertion.assertion_id,
                path="lifecycle.py",
                start_line=1,
                end_line=1,
            ),
        ),
        valid_citation_count=1,
        assertion_recall=1.0,
        citation_validity=1.0,
    )

    def respond(request: ModelRequest, _: int) -> str:
        evidence = request.untrusted_sources[1].text
        return json.dumps(
            {"schema_version": 1, "grounded": "resource.close()" in evidence}
        )

    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="http://127.0.0.1:1",
            model_id="dispose-judge",
            retry_limit=0,
            max_json_repair_attempts=0,
        ),
        responder=respond,
    )
    header_only = asyncio.run(
        _run_groundedness_judge(
            provider,
            (assertion,),
            answer,
            render_oracle_context(
                tmp_path,
                (BenchmarkSourceRange(path="lifecycle.py", start_line=1, end_line=1),),
            ),
        )
    )
    body_present = asyncio.run(
        _run_groundedness_judge(
            provider,
            (assertion,),
            answer,
            render_oracle_context(
                tmp_path,
                (BenchmarkSourceRange(path="lifecycle.py", start_line=1, end_line=2),),
            ),
        )
    )

    assert header_only.votes == (False, False, False)
    assert not header_only.passed
    assert body_present.votes == (True, True, True)
    assert body_present.passed
