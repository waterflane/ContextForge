import asyncio
import json
import subprocess
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from contextforge.benchmarks import (
    BenchmarkAnswerCitation,
    BenchmarkAnswerEvaluation,
    BenchmarkGroundednessEvaluation,
    BenchmarkPairedAnswerEvaluation,
    BenchmarkSourceRange,
    RealBenchmarkMode,
    RealBenchmarkObservation,
    aggregate_real_repository_report,
    evaluate_real_repository_observation,
    load_real_repository_benchmark_manifest,
    run_pinned_real_repository_benchmark,
    run_real_repository_benchmark,
)
from contextforge.benchmarks.real_repositories import (
    RealBenchmarkAggregate,
    RealBenchmarkBuildReport,
    RealBenchmarkTaskReport,
)
from contextforge.models import FakeModelProvider, ProviderConfiguration

MANIFEST = Path(__file__).parents[1] / "benchmarks" / "real-repository-v31.json"


def test_real_report_schema_tracks_public_fields() -> None:
    schema_path = (
        Path(__file__).parents[1]
        / "docs"
        / "schemas"
        / "real-repository-benchmark-v2.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert set(schema["$defs"]["run"]["properties"]) == set(
        RealBenchmarkTaskReport.model_fields
    )
    assert set(schema["$defs"]["aggregate"]["properties"]) == set(
        RealBenchmarkAggregate.model_fields
    )
    assert set(schema["$defs"]["build"]["properties"]) == set(
        RealBenchmarkBuildReport.model_fields
    )


def test_live_reload_compares_complete_candidate_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextforge.benchmarks.live_real_repositories as live_module

    outputs = iter(("stable\n", "stable\n", "different\n"))
    seeds: list[str] = []

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        script = args[0]
        assert isinstance(script, list)
        assert "c.model_dump(mode='json')" in script[3]
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        seeds.append(environment["PYTHONHASHSEED"])
        return subprocess.CompletedProcess(script, 0, next(outputs))

    monkeypatch.setattr(live_module.subprocess, "run", run)
    assert live_module._fresh_process_reloads(tmp_path, "alpha", attempts=3) == 2
    assert seeds == ["0", "1", "2"]
    monkeypatch.setattr(
        live_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, ""),
    )
    assert live_module._fresh_process_reloads(tmp_path, "alpha", attempts=1) == 0


def test_live_materialization_rejects_an_uncompiled_value() -> None:
    import contextforge.benchmarks.live_real_repositories as live_module

    with pytest.raises(TypeError, match="compiled capsule expected"):
        live_module._materialized(object())


@pytest.mark.parametrize(
    ("message", "reason"),
    (
        ("reviewed range is stale: alpha.py", "reviewed_range_stale"),
        ("reviewed evidence references an absent source", "reviewed_source_absent"),
        (
            "reviewed evidence ID is stale or outside its range",
            "reviewed_evidence_stale",
        ),
        ("reviewed evidence has no retrieval index", "retrieval_index_absent"),
        ("unrelated failure", "pipeline_error:ValueError"),
    ),
)
def test_live_failure_reasons_are_bounded(message: str, reason: str) -> None:
    import contextforge.benchmarks.live_real_repositories as live_module

    assert live_module._failure_reason(ValueError(message)) == reason


def test_live_meter_does_not_count_open_circuit_as_http() -> None:
    from pydantic import BaseModel, ConfigDict

    import contextforge.benchmarks.live_real_repositories as live_module
    from contextforge.models import ModelRequest, ProviderCircuitOpenError

    class Response(BaseModel):
        model_config = ConfigDict(extra="forbid")

        schema_version: Literal[1] = 1

    class OpenCircuitProvider:
        configuration = ProviderConfiguration(
            provider_id="openai-compatible",
            endpoint="http://127.0.0.1:1919/v1",
            model_id="fixture",
        )

        async def complete_structured(self, *_: object, **__: object) -> None:
            raise ProviderCircuitOpenError("provider circuit opened")

    metered = live_module._MeasuredProvider(OpenCircuitProvider())
    request = ModelRequest(
        operation_id="benchmark-test",
        purpose="test",
        system_instructions="system",
        analysis_task="task",
        trusted_code_map_facts={},
        untrusted_sources=(),
        response_model=Response,
    )
    with pytest.raises(ProviderCircuitOpenError):
        asyncio.run(metered.complete_structured(request))
    assert metered.calls == 0
    assert metered.estimated_input > 0


def _paired_answer(
    *,
    evidence_support: float = 1.0,
    ordinary_tokens: int = 100,
    capsule_tokens: int = 70,
) -> BenchmarkPairedAnswerEvaluation:
    citation = BenchmarkAnswerCitation(
        assertion_id="alpha",
        path="alpha.py",
        start_line=1,
        end_line=1,
        material_evidence_ids=("symbol:0000",),
    )
    oracle = BenchmarkAnswerEvaluation(
        answer="Alpha is present.",
        assertion_ids=("alpha",),
        citations=(citation,),
        valid_citation_count=1,
        assertion_recall=1.0,
        citation_validity=1.0,
        assertion_evidence_support=1.0,
        lexical_identifier_support=1.0,
        input_tokens=40,
        provider_http_calls=1,
    )
    contextforge = oracle.model_copy(
        update={
            "input_tokens": capsule_tokens,
            "assertion_evidence_support": evidence_support,
        }
    )
    return BenchmarkPairedAnswerEvaluation(
        ordinary=oracle.model_copy(update={"input_tokens": ordinary_tokens}),
        oracle=oracle,
        contextforge=contextforge,
        contextforge_groundedness=BenchmarkGroundednessEvaluation(
            votes=(True, True, True), passed=True, provider_http_calls=3
        ),
        input_token_reduction=(ordinary_tokens - capsule_tokens) / ordinary_tokens,
        quality_not_lower=evidence_support == 1.0,
    )


def _observation(
    *, materialized: tuple[str, ...], mode: RealBenchmarkMode
) -> RealBenchmarkObservation:
    return RealBenchmarkObservation(
        mode=mode,
        retrieved_top5=("alpha.py",),
        materialized_files=materialized,
        capsule_tokens=70,
        ordinary_tokens=100,
        paired_answer=_paired_answer(),
        planner_calls=0 if mode is RealBenchmarkMode.DETERMINISTIC else 3,
        planner_input_tokens=0 if mode is RealBenchmarkMode.DETERMINISTIC else 120,
        latency_ms=18,
        plan_sufficient=None if mode is RealBenchmarkMode.DETERMINISTIC else True,
        citation_validity=1.0,
        assertion_recall=1.0,
        assertion_evidence_support=1.0,
        lexical_identifier_support=1.0,
        groundedness_majority=True,
        quality_not_lower_than_oracle=True,
    )


def test_real_manifest_covers_four_repositories_and_sixteen_tasks() -> None:
    manifest = load_real_repository_benchmark_manifest(MANIFEST)

    assert len(manifest.repositories) == 4
    assert len(manifest.tasks) == 16
    assert sum(task.kind.value == "broad" for task in manifest.tasks) == 13
    assert sum(task.kind.value == "exact_symbol" for task in manifest.tasks) == 3
    assert {task.dataset_split for task in manifest.tasks} == {"tuning", "holdout"}
    assert all(task.task_roles and task.answer_assertions for task in manifest.tasks)
    assert all(
        {path for role in task.task_roles for path in role.required_paths}
        == set(task.required_files)
        for task in manifest.tasks
    )


def test_quality_gate_excludes_savings_when_materialized_recall_is_baseline_red() -> (
    None
):
    manifest = load_real_repository_benchmark_manifest(MANIFEST)
    task = manifest.tasks[0]
    observation = RealBenchmarkObservation(
        mode=RealBenchmarkMode.PLANNED,
        retrieved_top5=task.required_files,
        materialized_files=(task.required_files[0],),
        capsule_tokens=10,
        ordinary_tokens=100,
        planner_calls=3,
        planner_input_tokens=200,
        planner_output_tokens=40,
        latency_ms=10,
        plan_sufficient=False,
        citation_validity=1.0,
        groundedness_majority=True,
        quality_not_lower_than_oracle=True,
    )

    run = evaluate_real_repository_observation(task, observation)
    report = aggregate_real_repository_report(
        manifest.model_copy(update={"tasks": (task,)}), (run,)
    )

    assert run.materialized_required_file_recall == 1 / len(task.required_files)
    assert run.quality_gate_failed is True
    assert report.passed is False
    assert report.aggregates[1].headline_token_savings == 0.0
    assert report.aggregates[1].quality_gate_failed_count == 1


def test_quality_gate_rejects_low_candidate_precision_even_with_full_material() -> None:
    task = load_real_repository_benchmark_manifest(MANIFEST).tasks[0]
    observation = RealBenchmarkObservation(
        mode=RealBenchmarkMode.DETERMINISTIC,
        retrieved_top5=(*task.required_files, "unrelated.py"),
        materialized_files=task.required_files,
        capsule_tokens=10,
        ordinary_tokens=100,
        citation_validity=1.0,
        groundedness_majority=True,
        quality_not_lower_than_oracle=True,
    )

    run = evaluate_real_repository_observation(task, observation)

    assert run.required_file_recall_at_5 == 1.0
    assert run.materialized_required_file_recall == 1.0
    assert run.precision_at_5 == 0.6
    assert run.precision_at_r == 1.0
    assert run.quality_gate_failed is True
    assert run.valid_token_savings == 0.0


@pytest.mark.parametrize(
    ("mode", "calls"),
    [
        (RealBenchmarkMode.DETERMINISTIC, 1),
        (RealBenchmarkMode.PLANNED, 4),
    ],
)
def test_quality_gate_rejects_provider_call_budget_violation(
    mode: RealBenchmarkMode, calls: int
) -> None:
    task = load_real_repository_benchmark_manifest(MANIFEST).tasks[0]
    observation = RealBenchmarkObservation(
        mode=mode,
        retrieved_top5=task.required_files,
        materialized_files=task.required_files,
        capsule_tokens=10,
        ordinary_tokens=100,
        planner_calls=calls,
        citation_validity=1.0,
        groundedness_majority=True,
        quality_not_lower_than_oracle=True,
    )

    assert evaluate_real_repository_observation(task, observation).quality_gate_failed


def test_planned_savings_require_effective_sufficiency() -> None:
    task = load_real_repository_benchmark_manifest(MANIFEST).tasks[0]
    observation = RealBenchmarkObservation(
        mode=RealBenchmarkMode.PLANNED,
        retrieved_top5=task.required_files,
        materialized_files=task.required_files,
        capsule_tokens=10,
        ordinary_tokens=100,
        citation_validity=1.0,
        assertion_recall=1.0,
        assertion_evidence_support=1.0,
        lexical_identifier_support=1.0,
        groundedness_majority=True,
        quality_not_lower_than_oracle=True,
        plan_sufficient=False,
        paired_answer=_paired_answer(),
    )

    report = evaluate_real_repository_observation(task, observation)

    assert report.quality_gate_failed
    assert report.valid_token_savings == 0.0


def test_citation_containment_alone_does_not_pass_assertion_support() -> None:
    task = load_real_repository_benchmark_manifest(MANIFEST).tasks[0]
    observation = RealBenchmarkObservation(
        mode=RealBenchmarkMode.DETERMINISTIC,
        retrieved_top5=task.required_files,
        materialized_files=task.required_files,
        capsule_tokens=10,
        ordinary_tokens=100,
        citation_validity=1.0,
        assertion_recall=1.0,
        assertion_evidence_support=0.0,
        lexical_identifier_support=1.0,
        groundedness_majority=True,
        quality_not_lower_than_oracle=True,
        paired_answer=_paired_answer(evidence_support=0.0),
    )

    result = evaluate_real_repository_observation(task, observation)

    assert result.citation_validity == 1.0
    assert result.quality_gate_failed
    assert result.valid_token_savings == 0.0


def test_final_answer_over_ninety_seconds_fails_quality_gate() -> None:
    task = load_real_repository_benchmark_manifest(MANIFEST).tasks[0]
    paired = _paired_answer()
    observation = RealBenchmarkObservation(
        mode=RealBenchmarkMode.DETERMINISTIC,
        retrieved_top5=task.required_files,
        materialized_files=task.required_files,
        materialized_ranges=task.required_ranges,
        capsule_tokens=70,
        ordinary_tokens=100,
        paired_answer=paired.model_copy(
            update={
                "contextforge": paired.contextforge.model_copy(
                    update={"duration_ms": 90_001}
                )
            }
        ),
    )

    result = evaluate_real_repository_observation(task, observation)

    assert result.quality_gate_failed
    assert result.valid_token_savings == 0.0


def test_final_answer_tokens_override_capsule_size_and_legacy_flags() -> None:
    task = load_real_repository_benchmark_manifest(MANIFEST).tasks[0]
    observation = RealBenchmarkObservation(
        mode=RealBenchmarkMode.DETERMINISTIC,
        retrieved_top5=task.required_files,
        materialized_files=task.required_files,
        capsule_tokens=10,
        ordinary_tokens=100,
        citation_validity=0.0,
        assertion_recall=0.0,
        paired_answer=_paired_answer(ordinary_tokens=100, capsule_tokens=110),
    )

    result = evaluate_real_repository_observation(task, observation)

    assert result.capsule_tokens == 110
    assert result.final_answer_input_tokens == 110
    assert result.ordinary_tokens == 100
    assert result.token_savings == pytest.approx(-0.1)
    assert result.citation_validity == 1.0
    assert result.paired_answer is not None
    assert result.paired_answer.contextforge.citations[0].material_evidence_ids == (
        "symbol:0000",
    )


def test_headline_uses_mean_of_quality_passing_answer_reductions() -> None:
    manifest = load_real_repository_benchmark_manifest(MANIFEST)
    tasks = manifest.tasks[:2]
    observations = (
        _observation(
            materialized=tasks[0].required_files,
            mode=RealBenchmarkMode.DETERMINISTIC,
        ).model_copy(
            update={
                "retrieved_top5": tasks[0].required_files,
                "materialized_ranges": tasks[0].required_ranges,
            }
        ),
        _observation(
            materialized=tasks[1].required_files,
            mode=RealBenchmarkMode.DETERMINISTIC,
        ).model_copy(
            update={
                "retrieved_top5": tasks[1].required_files,
                "materialized_ranges": tasks[1].required_ranges,
                "paired_answer": _paired_answer(
                    ordinary_tokens=1000, capsule_tokens=500
                ),
            }
        ),
    )
    reports = tuple(
        evaluate_real_repository_observation(task, observation)
        for task, observation in zip(tasks, observations, strict=True)
    )
    reduced = manifest.model_copy(update={"tasks": tasks})

    result = aggregate_real_repository_report(reduced, reports)

    assert result.aggregates[0].headline_token_savings == pytest.approx(0.4)
    assert result.passed is False  # callback observations are not live verification


def test_v2_manifest_requires_reviewed_ranges_and_evidence_ids() -> None:
    manifest = load_real_repository_benchmark_manifest(MANIFEST)
    payload = manifest.model_dump(mode="json")
    payload["schema_version"] = 2
    for task in payload["tasks"]:
        task["required_ranges"] = []
        task["oracle_ranges"] = []
        task["answer_assertions"][0]["support"] = []
    with pytest.raises(ValidationError, match="required and manual oracle ranges"):
        type(manifest).model_validate_json(json.dumps(payload))

    for task in payload["tasks"]:
        spans = [
            {"path": path, "start_line": 1, "end_line": 2}
            for path in sorted(task["required_files"])
        ]
        task["required_ranges"] = spans
        task["oracle_ranges"] = spans
    with pytest.raises(ValidationError, match="reviewed support"):
        type(manifest).model_validate_json(json.dumps(payload))

    for task in payload["tasks"]:
        task["answer_assertions"][0]["support"] = [
            {
                "citation": task["oracle_ranges"][0],
                "material_evidence_ids": ["symbol:0000"],
            }
        ]
    assert type(manifest).model_validate_json(json.dumps(payload)).schema_version == 2


def test_semantic_counts_must_be_complete_and_bounded() -> None:
    with pytest.raises(ValidationError, match="reported together"):
        RealBenchmarkObservation(
            mode=RealBenchmarkMode.DETERMINISTIC,
            retrieved_top5=(),
            materialized_files=(),
            capsule_tokens=0,
            ordinary_tokens=0,
            semantic_requested_functions=4,
        )
    with pytest.raises(ValidationError, match="cannot exceed requested"):
        RealBenchmarkObservation(
            mode=RealBenchmarkMode.DETERMINISTIC,
            retrieved_top5=(),
            materialized_files=(),
            capsule_tokens=0,
            ordinary_tokens=0,
            semantic_requested_functions=4,
            semantic_described_functions=5,
            semantic_file_only_functions=0,
        )


def test_observation_rejects_more_than_five_top_five_candidates() -> None:
    with pytest.raises(ValidationError, match="at most 5"):
        RealBenchmarkObservation(
            mode=RealBenchmarkMode.DETERMINISTIC,
            retrieved_top5=tuple(f"candidate-{index}.py" for index in range(6)),
            materialized_files=(),
            capsule_tokens=0,
            ordinary_tokens=1,
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"required_files": ["alpha.py", "alpha.py"]}, "unique"),
        (
            {
                "task_roles": [
                    {
                        "role_id": "only",
                        "description": "One role.",
                        "required_paths": ["alpha.py"],
                    }
                ]
            },
            "cover exactly",
        ),
        (
            {
                "required_ranges": [
                    {"path": "missing.py", "start_line": 1, "end_line": 2}
                ]
            },
            "refer to required_files",
        ),
        (
            {
                "answer_assertions": [
                    {"assertion_id": "z", "description": "One."},
                    {"assertion_id": "a", "description": "Two."},
                ]
            },
            "canonical unique",
        ),
    ],
)
def test_real_manifest_rejects_inconsistent_task_evidence(
    change: dict[str, object], message: str
) -> None:
    task = load_real_repository_benchmark_manifest(MANIFEST).tasks[0]
    payload = task.model_dump(mode="json")
    payload.update(change)
    with pytest.raises(ValidationError, match=message):
        type(task).model_validate_json(json.dumps(payload))


def test_real_manifest_rejects_duplicate_role_paths_and_ranges() -> None:
    task = load_real_repository_benchmark_manifest(MANIFEST).tasks[0]
    payload = task.model_dump(mode="json")
    path = task.required_files[0]
    payload["task_roles"][0]["required_paths"] = [path, path]
    with pytest.raises(ValidationError, match="sorted and unique"):
        type(task).model_validate_json(json.dumps(payload))
    payload = task.model_dump(mode="json")
    span = {"path": path, "start_line": 1, "end_line": 2}
    payload["required_ranges"] = [span, span]
    with pytest.raises(ValidationError, match="sorted and unique"):
        type(task).model_validate_json(json.dumps(payload))


@pytest.mark.parametrize("mutation", ["repositories", "tasks", "unknown_repository"])
def test_real_manifest_rejects_ambiguous_identity(mutation: str) -> None:
    manifest = load_real_repository_benchmark_manifest(MANIFEST)
    payload = manifest.model_dump(mode="json")
    if mutation == "repositories":
        payload["repositories"].append(payload["repositories"][0])
    elif mutation == "tasks":
        payload["tasks"].append(payload["tasks"][0])
    else:
        payload["tasks"][0]["repository_id"] = "unknown"
    with pytest.raises(ValidationError):
        type(manifest).model_validate_json(json.dumps(payload))


def test_range_recall_merges_overlap_but_preserves_gaps() -> None:
    manifest = load_real_repository_benchmark_manifest(MANIFEST)
    task = next(item for item in manifest.tasks if item.required_ranges)
    required = task.required_ranges[0]
    first = BenchmarkSourceRange(
        path=required.path,
        start_line=required.start_line,
        end_line=required.start_line + 2,
    )
    overlap = BenchmarkSourceRange(
        path=required.path,
        start_line=required.start_line + 2,
        end_line=required.end_line,
    )
    complete = RealBenchmarkObservation(
        mode=RealBenchmarkMode.DETERMINISTIC,
        retrieved_top5=task.required_files,
        materialized_files=task.required_files,
        materialized_ranges=(first, overlap, *task.required_ranges[1:]),
        capsule_tokens=70,
        ordinary_tokens=100,
        citation_validity=1.0,
        assertion_recall=1.0,
        assertion_evidence_support=1.0,
        lexical_identifier_support=1.0,
        groundedness_majority=True,
        quality_not_lower_than_oracle=True,
    )
    assert evaluate_real_repository_observation(task, complete).range_recall == 1.0
    gap = first.model_copy(update={"end_line": required.start_line})
    partial = complete.model_copy(
        update={"materialized_ranges": (gap, overlap, *task.required_ranges[1:])}
    )
    result = evaluate_real_repository_observation(task, partial)
    assert result.range_recall is not None and result.range_recall < 1.0
    insufficient = complete.model_copy(
        update={"materialized_ranges": (gap, *task.required_ranges[1:])}
    )
    assert evaluate_real_repository_observation(task, insufficient).quality_gate_failed
    empty = complete.model_copy(update={"materialized_ranges": ()})
    assert evaluate_real_repository_observation(task, empty).range_recall == 0.0


def test_observation_rejects_duplicate_observed_paths() -> None:
    with pytest.raises(ValidationError, match="observed paths must be unique"):
        RealBenchmarkObservation(
            mode=RealBenchmarkMode.DETERMINISTIC,
            retrieved_top5=("alpha.py", "alpha.py"),
            materialized_files=(),
            capsule_tokens=1,
            ordinary_tokens=1,
        )


def test_report_rejects_missing_task_and_invalid_skip_status() -> None:
    manifest = load_real_repository_benchmark_manifest(MANIFEST)
    task = manifest.tasks[0]
    complete = evaluate_real_repository_observation(
        task,
        RealBenchmarkObservation(
            mode=RealBenchmarkMode.DETERMINISTIC,
            retrieved_top5=task.required_files,
            materialized_files=task.required_files,
            capsule_tokens=1,
            ordinary_tokens=2,
        ),
    )
    with pytest.raises(ValueError, match="exactly one run"):
        aggregate_real_repository_report(manifest, (complete,))
    payload = complete.model_dump(mode="json")
    payload.update(status="skipped", skip_reason=None)
    with pytest.raises(ValidationError, match="skipped result"):
        type(complete).model_validate_json(json.dumps(payload))
    payload.update(status="complete", skip_reason="unexpected")
    with pytest.raises(ValidationError, match="complete result"):
        type(complete).model_validate_json(json.dumps(payload))


def test_aggregation_is_deterministic_and_separates_modes() -> None:
    manifest = load_real_repository_benchmark_manifest(MANIFEST)
    tasks = manifest.tasks[:2]
    first = evaluate_real_repository_observation(
        tasks[0],
        _observation(
            materialized=("alpha.py", "beta.py"), mode=RealBenchmarkMode.DETERMINISTIC
        ),
    )
    second = evaluate_real_repository_observation(
        tasks[1],
        _observation(
            materialized=("alpha.py", "beta.py"), mode=RealBenchmarkMode.PLANNED
        ),
    )
    reduced = manifest.model_copy(update={"tasks": tasks})

    one = aggregate_real_repository_report(reduced, (second, first))
    two = aggregate_real_repository_report(reduced, (second, first))

    assert one.model_dump_json() == two.model_dump_json()
    deterministic, planned = one.aggregates
    assert deterministic.mode is RealBenchmarkMode.DETERMINISTIC
    assert deterministic.planner_calls == 0
    assert planned.mode is RealBenchmarkMode.PLANNED
    assert planned.planner_calls == 3
    assert planned.planner_input_tokens == 120


def test_aggregation_reports_phases_and_rejects_missing_repeat() -> None:
    manifest = load_real_repository_benchmark_manifest(MANIFEST)
    task = manifest.tasks[0]
    reduced = manifest.model_copy(update={"tasks": (task,)})
    observation = RealBenchmarkObservation(
        mode=RealBenchmarkMode.DETERMINISTIC,
        retrieved_top5=task.required_files,
        materialized_files=task.required_files,
        capsule_tokens=50,
        ordinary_tokens=100,
        citation_validity=1.0,
        groundedness_majority=True,
        quality_not_lower_than_oracle=True,
        cold_structural_ms=10,
        semantic_offline_ms=20,
        deterministic_warm_query_ms=30,
        agentic_planner_ms=0,
        final_answer_ms=40,
        semantic_provider_calls=2,
        semantic_input_tokens=400,
        semantic_output_tokens=100,
        final_answer_input_tokens=50,
        final_answer_output_tokens=20,
        semantic_requested_functions=4,
        semantic_described_functions=3,
        semantic_file_only_functions=1,
    )
    runs = tuple(
        evaluate_real_repository_observation(task, observation, repetition=index)
        for index in range(1, 4)
    )

    report = aggregate_real_repository_report(reduced, runs)

    assert tuple(item.repetition for item in report.runs) == (1, 2, 3)
    deterministic = report.aggregates[0]
    assert deterministic.mean_cold_structural_ms == 10
    assert deterministic.mean_semantic_offline_ms == 20
    assert deterministic.mean_deterministic_warm_query_ms == 30
    assert deterministic.mean_final_answer_ms == 40
    assert deterministic.semantic_provider_calls == 6
    assert deterministic.semantic_input_tokens == 1200
    assert deterministic.semantic_function_coverage == 0.75
    assert deterministic.semantic_file_only_rate == 1 / 3
    with pytest.raises(ValueError, match="repetition"):
        aggregate_real_repository_report(reduced, (runs[0], runs[2]))


def test_missing_external_source_is_skip_and_never_a_false_pass() -> None:
    manifest = load_real_repository_benchmark_manifest(MANIFEST)
    task = manifest.tasks[0]
    reduced = manifest.model_copy(update={"tasks": (task,)})

    report = asyncio.run(run_real_repository_benchmark(reduced, {}, lambda *_: None))

    assert report.passed is False
    assert report.runs[0].status == "skipped"
    assert report.runs[0].skip_reason == "external_source_missing"
    assert report.runs[0].quality_gate_failed is True


def test_official_runner_requires_reviewed_manifest_and_reports_missing_source() -> (
    None
):
    manifest = load_real_repository_benchmark_manifest(MANIFEST)
    configuration = ProviderConfiguration(
        provider_id="openai-compatible",
        endpoint="http://127.0.0.1:1919/v1",
        model_id="Qwen3.6-35B-A3B-NVFP4",
        context_window=16_384,
        reasoning_effort="off",
    )
    with pytest.raises(ValueError, match="reviewed v2"):
        asyncio.run(
            run_pinned_real_repository_benchmark(
                manifest.model_copy(update={"schema_version": 1}),
                {},
                configuration,
                repetitions=1,
            )
        )
    with pytest.raises(ValueError, match="semantic file limit"):
        asyncio.run(
            run_pinned_real_repository_benchmark(
                manifest, {}, configuration, semantic_max_files=0
            )
        )
    with pytest.raises(ValueError, match="semantic request limit"):
        asyncio.run(
            run_pinned_real_repository_benchmark(
                manifest, {}, configuration, semantic_max_requests=0
            )
        )
    payload = manifest.model_dump(mode="json")
    payload["schema_version"] = 2
    payload["repositories"] = payload["repositories"][:1]
    task = next(
        item
        for item in payload["tasks"]
        if item["repository_id"] == payload["repositories"][0]["repository_id"]
    )
    payload["tasks"] = [task]
    spans = [
        {"path": path, "start_line": 1, "end_line": 2}
        for path in sorted(task["required_files"])
    ]
    span = spans[0]
    task["required_ranges"] = spans
    task["oracle_ranges"] = spans
    task["answer_assertions"][0]["support"] = [
        {"citation": span, "material_evidence_ids": ["symbol:0000"]}
    ]
    reviewed = type(manifest).model_validate_json(json.dumps(payload))

    report = asyncio.run(
        run_pinned_real_repository_benchmark(reviewed, {}, configuration, repetitions=1)
    )

    assert report.verified_pipeline
    assert not report.passed
    assert len(report.runs) == 2
    assert {run.mode for run in report.runs} == set(RealBenchmarkMode)
    assert all(run.skip_reason == "external_source_missing" for run in report.runs)
    assert report.builds[0].status == "skipped"
    assert report.provider_endpoint == "http://127.0.0.1:1919/v1"


def test_official_runner_executes_built_in_pipeline_on_pinned_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextforge.benchmarks.live_real_repositories as live_module
    from contextforge.application import build_repository_index
    from contextforge.intelligence.retrieval import load_retrieval_index

    source = tmp_path / "source"
    source.mkdir()
    (source / "alpha.py").write_text(
        'def alpha():\n    return "hello"\n', encoding="utf-8"
    )
    (source / ".gitignore").write_text(".contextforge/\n", encoding="utf-8")
    for command in (
        ("git", "init", "--quiet", str(source)),
        ("git", "-C", str(source), "config", "user.email", "benchmark@example.test"),
        ("git", "-C", str(source), "config", "user.name", "Benchmark"),
        ("git", "-C", str(source), "add", "alpha.py", ".gitignore"),
        ("git", "-C", str(source), "commit", "--quiet", "-m", "fixture"),
    ):
        subprocess.run(command, check=True, capture_output=True, text=True)
    revision = subprocess.run(
        ("git", "-C", str(source), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    structural = asyncio.run(
        build_repository_index(source, provider=None, provider_configuration=None)
    )
    reference = structural.manifest.artifacts.structural_retrieval
    assert reference is not None
    index = load_retrieval_index(source, reference, manifest=structural.manifest)
    evidence_id = next(
        item.evidence_id
        for document in index.documents
        if document.path == "alpha.py"
        for item in document.positional_postings
        if item.identifier == "alpha"
    )
    manifest = type(
        load_real_repository_benchmark_manifest(MANIFEST)
    ).model_validate_json(
        json.dumps(
            {
                "schema_version": 2,
                "suite_name": "built-in-fixture",
                "repositories": [{"repository_id": "fixture", "revision": revision}],
                "tasks": [
                    {
                        "task_id": "alpha",
                        "repository_id": "fixture",
                        "kind": "broad",
                        "dataset_split": "tuning",
                        "task": "Explain alpha.",
                        "required_files": ["alpha.py"],
                        "relevant_top5": ["alpha.py"],
                        "required_ranges": [
                            {"path": "alpha.py", "start_line": 1, "end_line": 2}
                        ],
                        "oracle_ranges": [
                            {"path": "alpha.py", "start_line": 1, "end_line": 2}
                        ],
                        "task_roles": [
                            {
                                "role_id": "implementation",
                                "description": "Defines alpha.",
                                "required_paths": ["alpha.py"],
                            }
                        ],
                        "answer_assertions": [
                            {
                                "assertion_id": "alpha",
                                "description": "alpha returns hello",
                                "support": [
                                    {
                                        "citation": {
                                            "path": "alpha.py",
                                            "start_line": 1,
                                            "end_line": 2,
                                        },
                                        "material_evidence_ids": [evidence_id],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        )
    )

    def responder(request, call):
        del call
        facts = request.trusted_code_map_facts
        if request.purpose.startswith("semantic-card"):
            return json.dumps(
                {
                    "schema_version": 1,
                    "synopsis": {
                        "text": "alpha returns hello",
                        "evidence_ids": ["file"],
                    },
                    "concepts": [{"text": "alpha hello", "evidence_ids": ["file"]}],
                    "responsibilities": [],
                    "key_symbols": [],
                    "side_effects": [],
                    "profile_facts": {},
                }
            )
        if request.purpose == "semantic-lexicon":
            return json.dumps(
                {
                    "schema_version": 1,
                    "functions": [
                        {
                            "symbol_id": item["symbol_id"],
                            "summary": "Returns hello",
                            "expressions": ["greeting function"],
                        }
                        for item in facts["target_functions"]
                    ],
                    "calls": [],
                }
            )
        if request.purpose == "semantic-lexicon-verification":
            return json.dumps(
                {
                    "schema_version": 1,
                    "accepted_ids": sorted(facts["proposed_claims"]),
                }
            )
        if request.purpose == "benchmark-groundedness-judge":
            return json.dumps(
                {"schema_version": 1, "grounded": True, "unsupported_claims": []}
            )
        if request.purpose == "benchmark-answer-regression":
            allowed = facts["allowed_citation_ranges"]
            material = facts["material_evidence"]
            citations = (
                [
                    {
                        "assertion_id": "alpha",
                        **allowed[0],
                        "material_evidence_ids": (
                            material[0]["evidence_ids"] if material else []
                        ),
                    }
                ]
                if allowed
                else []
            )
            return json.dumps(
                {
                    "schema_version": 1,
                    "answer": "alpha returns hello"
                    if citations
                    else "Insufficient evidence",
                    "assertion_ids": ["alpha"] if citations else [],
                    "citations": citations,
                }
            )
        return json.dumps(
            {"schema_version": 1, "selected": [], "sufficiency": "insufficient"}
        )

    def fake_provider(_configuration):
        return FakeModelProvider(
            ProviderConfiguration(
                provider_id="fake",
                endpoint="http://127.0.0.1:1",
                model_id="benchmark-fake",
                context_window=16_384,
                retry_limit=0,
                max_json_repair_attempts=0,
            ),
            responder=responder,
        )

    monkeypatch.setattr(live_module, "_new_provider", fake_provider)
    configuration = ProviderConfiguration(
        provider_id="openai-compatible",
        endpoint="http://127.0.0.1:1919/v1",
        model_id="Qwen3.6-35B-A3B-NVFP4",
        context_window=16_384,
        reasoning_effort="off",
    )
    report = asyncio.run(
        run_pinned_real_repository_benchmark(
            manifest,
            {"fixture": source},
            configuration,
            repetitions=1,
            hash_seed_reloads=0,
        )
    )
    assert not report.passed
    assert report.builds[0].status == "complete"
    assert report.builds[0].requested_functions == 1
    assert report.builds[0].described_functions == 1
    assert report.builds[0].code_test_files == 1
    assert report.builds[0].tagged_code_test_files == 1
    assert report.builds[0].model_tagged_code_test_files == 1
    assert report.builds[0].noop_generation_unchanged
    assert {run.mode for run in report.runs} == set(RealBenchmarkMode)
    assert all(run.status == "complete" for run in report.runs)
    assert all(
        run.final_answer_ms == run.paired_answer.contextforge.duration_ms
        for run in report.runs
        if run.paired_answer is not None
    )
    assert all(
        run.final_answer_provider_calls
        == run.paired_answer.contextforge.provider_http_calls
        for run in report.runs
        if run.paired_answer is not None
    )

    async def fail_answers(*args: object, **kwargs: object) -> None:
        from contextforge.models import ProviderCircuitOpenError

        raise ProviderCircuitOpenError("provider circuit opened")

    monkeypatch.setattr(live_module, "run_paired_answer_regression", fail_answers)
    degraded = asyncio.run(
        run_pinned_real_repository_benchmark(
            manifest,
            {"fixture": source},
            configuration,
            repetitions=1,
            hash_seed_reloads=0,
        )
    )
    assert all(run.status == "complete" for run in degraded.runs)
    assert all(run.quality_gate_failed for run in degraded.runs)
    assert all(run.final_answer_ms is None for run in degraded.runs)
    assert all(run.required_file_recall_at_5 == 1 for run in degraded.runs)
    assert all(
        run.phase_errors == ("final_answer:pipeline_error:ProviderCircuitOpenError",)
        for run in degraded.runs
    )

    def failed_semantic_responder(request, call):
        if request.purpose.startswith("semantic-card"):
            return "{}"
        return responder(request, call)

    monkeypatch.setattr(
        live_module,
        "_new_provider",
        lambda _configuration: FakeModelProvider(
            fake_provider(None).configuration,
            responder=failed_semantic_responder,
        ),
    )
    partial = asyncio.run(
        run_pinned_real_repository_benchmark(
            manifest,
            {"fixture": source},
            configuration,
            repetitions=1,
            hash_seed_reloads=0,
        )
    )
    assert partial.builds[0].status == "partial"
    assert partial.builds[0].noop_update_ms is None
    assert partial.builds[0].noop_provider_calls is None
    task = manifest.tasks[0]
    stale_range = BenchmarkSourceRange(path="alpha.py", start_line=1, end_line=99)
    with pytest.raises(ValueError, match="reviewed range is stale"):
        live_module._validate_reviewed_sources(
            source, task.model_copy(update={"required_ranges": (stale_range,)})
        )
    assertion = task.answer_assertions[0]
    support = assertion.support[0].model_copy(
        update={"material_evidence_ids": ("unknown-evidence",)}
    )
    stale_evidence = task.model_copy(
        update={
            "answer_assertions": (assertion.model_copy(update={"support": (support,)}),)
        }
    )
    with pytest.raises(ValueError, match="reviewed evidence ID is stale"):
        live_module._validate_reviewed_sources(source, stale_evidence)


def test_corrupt_external_source_is_skip_and_never_a_false_pass(
    tmp_path: Path,
) -> None:
    manifest = load_real_repository_benchmark_manifest(MANIFEST)
    task = manifest.tasks[0]
    reduced = manifest.model_copy(update={"tasks": (task,)})
    corrupt_source = tmp_path / "not-a-git-repository"
    corrupt_source.mkdir()

    report = asyncio.run(
        run_real_repository_benchmark(
            reduced,
            {task.repository_id: corrupt_source},
            lambda *_: None,
        )
    )

    assert report.passed is False
    assert report.runs[0].status == "skipped"
    assert report.runs[0].skip_reason == "external_source_unavailable"


def test_live_harness_clones_a_pinned_repository_and_removes_the_fixture(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "alpha.py").write_text("alpha\n", encoding="utf-8")
    for command in (
        ("git", "init", "--quiet", str(source)),
        ("git", "-C", str(source), "config", "user.email", "benchmark@example.test"),
        ("git", "-C", str(source), "config", "user.name", "Benchmark"),
        ("git", "-C", str(source), "add", "alpha.py"),
        ("git", "-C", str(source), "commit", "--quiet", "-m", "fixture"),
    ):
        subprocess.run(command, check=True, capture_output=True, text=True)
    revision = subprocess.run(
        ("git", "-C", str(source), "rev-parse", "HEAD"),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    payload = {
        "schema_version": 1,
        "suite_name": "fixture",
        "repositories": [{"repository_id": "fixture", "revision": revision}],
        "tasks": [
            {
                "task_id": "fixture-task",
                "repository_id": "fixture",
                "kind": "broad",
                "dataset_split": "tuning",
                "task": "Inspect alpha.",
                "required_files": ["alpha.py"],
                "relevant_top5": ["alpha.py"],
                "task_roles": [
                    {
                        "role_id": "implementation",
                        "description": "Contains alpha.",
                        "required_paths": ["alpha.py"],
                    }
                ],
                "answer_assertions": [
                    {"assertion_id": "alpha", "description": "The file is present."}
                ],
            }
        ],
    }
    manifest = load_real_repository_benchmark_manifest(
        _write_json(tmp_path / "manifest.json", payload)
    )
    observed_roots: list[Path] = []

    def evaluator(root: Path, _task: object) -> RealBenchmarkObservation:
        observed_roots.append(root)
        assert (root / "alpha.py").read_text(encoding="utf-8") == "alpha\n"
        return _observation(
            materialized=("alpha.py",), mode=RealBenchmarkMode.DETERMINISTIC
        )

    report = asyncio.run(
        run_real_repository_benchmark(
            manifest, {"fixture": source}, evaluator, repetitions=3
        )
    )

    assert report.passed is False
    assert report.verified_pipeline is False
    assert len(observed_roots) == 3
    assert len(set(observed_roots)) == 3
    assert tuple(run.repetition for run in report.runs) == (1, 2, 3)
    assert all(not root.exists() for root in observed_roots)
    assert (source / "alpha.py").read_text(encoding="utf-8") == "alpha\n"

    def broken_evaluator(_root: Path, _task: object) -> RealBenchmarkObservation:
        raise ValueError("evaluator failed")

    with pytest.raises(ValueError, match="evaluator failed"):
        asyncio.run(
            run_real_repository_benchmark(
                manifest, {"fixture": source}, broken_evaluator
            )
        )


def _write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
