import asyncio
import json
from pathlib import Path

import pytest

from contextforge.application import build_repository_index
from contextforge.benchmarks import (
    BenchmarkExpectedAssertion,
    BenchmarkSourceRange,
    render_oracle_context,
    run_paired_answer_regression,
)
from contextforge.context import ContextBudget, compile_context_capsule
from contextforge.intelligence import retrieve_context_candidates
from contextforge.models import FakeModelProvider, ProviderConfiguration


def test_oracle_and_capsule_answers_keep_real_citation_identity(
    tmp_path: Path,
) -> None:
    source = "def serve(value: str) -> str:\n    return value.upper()\n"
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
    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="http://127.0.0.1:1",
            model_id="answer-test",
            retry_limit=0,
            max_json_repair_attempts=0,
        ),
        responder=lambda request, call: json.dumps(
            {
                "schema_version": 1,
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
        ),
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
    assert result.quality_not_lower is True


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
