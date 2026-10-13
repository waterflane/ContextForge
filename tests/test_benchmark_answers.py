import asyncio
import json
from pathlib import Path

import pytest

from contextforge.application import build_repository_index
from contextforge.benchmarks import (
    BenchmarkAnswerCitation,
    BenchmarkAnswerEvaluation,
    BenchmarkAssertionSupport,
    BenchmarkExpectedAssertion,
    BenchmarkSourceRange,
    render_oracle_context,
    render_ordinary_context,
    run_paired_answer_regression,
)
from contextforge.benchmarks.answers import (
    OrdinaryBaselineContextOverflow,
    _answer_request,
    _assertion_has_evidence_support,
    _run_groundedness_judge,
    _source_material_evidence,
)
from contextforge.context import ContextBudget, compile_context_capsule
from contextforge.intelligence import retrieve_context_candidates
from contextforge.intelligence.source_evidence import derive_source_evidence_units
from contextforge.models import FakeModelProvider, ModelRequest, ProviderConfiguration


@pytest.mark.parametrize(
    ("spans", "covered"),
    [
        (((1, 4),), True),
        (((1, 2), (3, 4)), True),
        (((1, 2),), False),
        (((1, 1), (3, 4)), False),
    ],
)
def test_source_material_exposes_units_only_with_complete_physical_coverage(
    tmp_path: Path, spans: tuple[tuple[int, int], ...], covered: bool
) -> None:
    (tmp_path / "service.py").write_text(
        "def serve(value):\n    prepared = value + 1\n"
        "    result = prepared * 2\n    return result\n",
        encoding="utf-8",
    )
    built = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    unit = next(
        u
        for u in derive_source_evidence_units(built.structural.code_maps[0])
        if u.kind == "implementation"
    )
    visible = tuple(
        BenchmarkSourceRange(path="service.py", start_line=a, end_line=b)
        for a, b in spans
    )
    material = _source_material_evidence(tmp_path, visible)
    assert tuple(r for r, _ in material) == visible
    assert any(unit.evidence_id in ids for _, ids in material) == covered


@pytest.mark.parametrize(
    ("spans", "supported"),
    [
        (((1, 4),), True),
        (((1, 2), (3, 4)), True),
        (((1, 1),), False),
        (((2, 4),), False),
    ],
)
def test_assertion_citations_must_cover_every_required_support_line(
    spans: tuple[tuple[int, int], ...], supported: bool
) -> None:
    source = BenchmarkSourceRange(path="service.py", start_line=1, end_line=4)
    assertion = BenchmarkExpectedAssertion(
        assertion_id="behavior",
        description="The implementation prepares and returns the result",
        support=(
            BenchmarkAssertionSupport(
                citation=source, material_evidence_ids=("implementation",)
            ),
        ),
    )
    citations = tuple(
        BenchmarkAnswerCitation(
            assertion_id="behavior",
            path="service.py",
            start_line=a,
            end_line=b,
            material_evidence_ids=("implementation",),
        )
        for a, b in spans
    )
    assert (
        _assertion_has_evidence_support(
            assertion, citations, ((source, ("implementation",)),)
        )
        == supported
    )


@pytest.mark.parametrize("damage", [None, "unknown", "foreign", "outside", "sha"])
def test_reviewed_validation_accepts_known_units_and_rejects_damaged_support(
    tmp_path: Path, damage: str | None
) -> None:
    from contextforge.benchmarks.live_real_repositories import (
        _validate_reviewed_sources,
    )
    from contextforge.benchmarks.real_repositories import (
        RealBenchmarkTask,
        RealBenchmarkTaskKind,
        RealBenchmarkTaskRole,
    )

    source = (
        "def serve(value):\n    prepared = value + 1\n"
        "    return prepared * 2\n# outside\n"
    )
    (tmp_path / "service.py").write_text(source, encoding="utf-8")
    (tmp_path / "other.py").write_text("def other():\n    return 0\n", encoding="utf-8")
    built = asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    maps = {m.path: m for m in built.structural.code_maps}
    unit = next(
        u
        for u in derive_source_evidence_units(maps["service.py"])
        if u.kind == "implementation"
    )
    identity = unit.evidence_id
    if damage == "unknown":
        identity = "guessed-id"
    elif damage == "foreign":
        identity = derive_source_evidence_units(maps["other.py"])[0].evidence_id
    required = BenchmarkSourceRange(
        path="service.py",
        start_line=4 if damage == "outside" else 1,
        end_line=4 if damage == "outside" else 3,
    )
    task = RealBenchmarkTask(
        task_id="unit-validation",
        repository_id="fixture",
        kind=RealBenchmarkTaskKind.EXACT_SYMBOL,
        dataset_split="holdout",
        task="Explain `serve`",
        required_files=("service.py",),
        relevant_top5=("service.py",),
        required_ranges=(required,),
        oracle_ranges=(required,),
        task_roles=(
            RealBenchmarkTaskRole(
                role_id="implementation",
                description="Implementation",
                required_paths=("service.py",),
            ),
        ),
        answer_assertions=(
            BenchmarkExpectedAssertion(
                assertion_id="behavior",
                description="serve returns the prepared value",
                support=(
                    BenchmarkAssertionSupport(
                        citation=required, material_evidence_ids=(identity,)
                    ),
                ),
            ),
        ),
    )
    if damage == "sha":
        (tmp_path / "service.py").write_text(
            source.replace("* 2", "* 3"), encoding="utf-8"
        )
        with pytest.raises(ValueError, match="material source SHA is stale"):
            _source_material_evidence(tmp_path, (required,))
    if damage is None:
        _validate_reviewed_sources(tmp_path, task)
    else:
        with pytest.raises(ValueError, match="stale or outside|source SHA is stale"):
            _validate_reviewed_sources(tmp_path, task)


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
    assert result.ordinary.provider_http_calls == 1
    assert result.input_token_reduction == pytest.approx(
        (result.ordinary.input_tokens - result.contextforge.input_tokens)
        / result.ordinary.input_tokens
    )
    assert seen_allowed_ranges == [
        [{"path": "service.py", "start_line": 1, "end_line": 42}],
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


def test_ordinary_context_overflow_is_reported_without_truncation(
    tmp_path: Path,
) -> None:
    (tmp_path / "service.py").write_text(
        "def serve():\n    return 1\n" + "# " + ("filler " * 8_000),
        encoding="utf-8",
    )
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
        budget=ContextBudget(
            context_window_tokens=16_384,
            response_tokens=512,
            safety_margin_tokens=256,
        ),
    )

    def unexpected_response(request: ModelRequest, call: int) -> str:
        raise AssertionError("ordinary overflow must be rejected before dispatch")

    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="http://127.0.0.1:1",
            model_id="answer-test",
            context_window=1_024,
            retry_limit=0,
            max_json_repair_attempts=0,
        ),
        responder=unexpected_response,
    )
    with pytest.raises(OrdinaryBaselineContextOverflow):
        asyncio.run(
            run_paired_answer_regression(
                tmp_path,
                "serve",
                (
                    BenchmarkExpectedAssertion(
                        assertion_id="serve", description="serve returns 1"
                    ),
                ),
                (BenchmarkSourceRange(path="service.py", start_line=1, end_line=2),),
                compiled,
                provider,
                ordinary_paths=("service.py",),
            )
        )
    assert provider.call_count == 0


def test_ordinary_renderer_uses_complete_required_files(tmp_path: Path) -> None:
    (tmp_path / "service.py").write_text(
        "first\nsecond\nthird\n", encoding="utf-8", newline=""
    )

    rendered, ranges = render_ordinary_context(tmp_path, ("service.py",))

    assert ranges == (
        BenchmarkSourceRange(path="service.py", start_line=1, end_line=3),
    )
    assert "first\nsecond\nthird" in rendered


def test_cross_file_assertion_requires_each_reviewed_support() -> None:
    first = BenchmarkSourceRange(path="caller.py", start_line=2, end_line=3)
    second = BenchmarkSourceRange(path="callee.py", start_line=4, end_line=5)
    assertion = BenchmarkExpectedAssertion(
        assertion_id="call-flow",
        description="caller invokes callee",
        support=(
            BenchmarkAssertionSupport(
                citation=first, material_evidence_ids=("caller-id",)
            ),
            BenchmarkAssertionSupport(
                citation=second, material_evidence_ids=("callee-id",)
            ),
        ),
    )
    caller = BenchmarkAnswerCitation(
        assertion_id="call-flow",
        path="caller.py",
        start_line=2,
        end_line=3,
        material_evidence_ids=("caller-id",),
    )
    callee = BenchmarkAnswerCitation(
        assertion_id="call-flow",
        path="callee.py",
        start_line=4,
        end_line=5,
        material_evidence_ids=("callee-id",),
    )
    material = ((first, ("caller-id",)), (second, ("callee-id",)))

    assert not _assertion_has_evidence_support(assertion, (caller,), material)
    assert _assertion_has_evidence_support(assertion, (caller, callee), material)
    assert not _assertion_has_evidence_support(
        assertion, (caller, callee), ((first, ("caller-id",)), (second, ()))
    )


def test_answer_request_isolates_reviewed_support(tmp_path: Path) -> None:
    (tmp_path / "service.py").write_text(
        "def serve():\n    return 1\n", encoding="utf-8"
    )
    asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    visible = (BenchmarkSourceRange(path="service.py", start_line=1, end_line=2),)
    assertion = BenchmarkExpectedAssertion(
        assertion_id="serve",
        description="serve returns 1",
        support=(
            BenchmarkAssertionSupport(
                citation=visible[0], material_evidence_ids=("secret-reviewed-id",)
            ),
        ),
    )
    material = _source_material_evidence(tmp_path, visible)
    assert material
    for label in ("ordinary-client", "manual-oracle", "contextforge-capsule"):
        request = _answer_request(
            "serve", (assertion,), "source", visible, material, label=label
        )
        facts = request.trusted_code_map_facts
        assert facts["assertions"] == [
            {"assertion_id": "serve", "description": "serve returns 1"}
        ]
        assert "secret-reviewed-id" not in json.dumps(facts)


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


def test_judge_request_isolates_reviewed_support() -> None:
    assertion = BenchmarkExpectedAssertion(
        assertion_id="behavior",
        description="The operation returns the value",
        support=(
            BenchmarkAssertionSupport(
                citation=BenchmarkSourceRange(
                    path="secret.py", start_line=100, end_line=120
                ),
                material_evidence_ids=("secret-reviewed-id",),
            ),
        ),
    )
    answer = BenchmarkAnswerEvaluation(
        answer="The operation returns the value.",
        assertion_ids=("behavior",),
        citations=(),
        valid_citation_count=0,
        assertion_recall=1.0,
        citation_validity=0.0,
    )

    def respond(request: ModelRequest, _: int) -> str:
        payload = json.loads(request.untrusted_sources[0].text)
        assert payload["assertions"] == [
            {"assertion_id": "behavior", "description": assertion.description}
        ]
        serialized = json.dumps(payload)
        assert "secret-reviewed-id" not in serialized
        assert "secret.py" not in serialized
        assert "support" not in serialized
        return '{"schema_version":1,"grounded":false}'

    provider = FakeModelProvider(
        ProviderConfiguration(
            provider_id="fake",
            endpoint="http://127.0.0.1:1",
            model_id="judge-isolation",
            retry_limit=0,
            max_json_repair_attempts=0,
        ),
        responder=respond,
    )
    judged = asyncio.run(
        _run_groundedness_judge(provider, (assertion,), answer, "visible source")
    )
    assert judged.votes == (False, False, False)
