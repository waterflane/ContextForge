import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from contextforge.benchmarks import (
    BenchmarkSourceRange,
    RealBenchmarkMode,
    RealBenchmarkObservation,
    aggregate_real_repository_report,
    evaluate_real_repository_observation,
    load_real_repository_benchmark_manifest,
    run_real_repository_benchmark,
)

MANIFEST = Path(__file__).parents[1] / "benchmarks" / "real-repository-v31.json"


def _observation(
    *, materialized: tuple[str, ...], mode: RealBenchmarkMode
) -> RealBenchmarkObservation:
    return RealBenchmarkObservation(
        mode=mode,
        retrieved_top5=("alpha.py",),
        materialized_files=materialized,
        capsule_tokens=70,
        ordinary_tokens=100,
        planner_calls=0 if mode is RealBenchmarkMode.DETERMINISTIC else 3,
        planner_input_tokens=0 if mode is RealBenchmarkMode.DETERMINISTIC else 120,
        latency_ms=18,
        plan_sufficient=None if mode is RealBenchmarkMode.DETERMINISTIC else True,
        citation_validity=1.0,
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
    assert run.precision_at_5 == 0.75
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
        ({"relevant_top5": ["missing.py"]}, "subset"),
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


def test_missing_external_source_is_skip_and_never_a_false_pass() -> None:
    manifest = load_real_repository_benchmark_manifest(MANIFEST)
    task = manifest.tasks[0]
    reduced = manifest.model_copy(update={"tasks": (task,)})

    report = asyncio.run(run_real_repository_benchmark(reduced, {}, lambda *_: None))

    assert report.passed is False
    assert report.runs[0].status == "skipped"
    assert report.runs[0].skip_reason == "external_source_missing"
    assert report.runs[0].quality_gate_failed is True


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
        run_real_repository_benchmark(manifest, {"fixture": source}, evaluator)
    )

    assert report.passed is True
    assert len(observed_roots) == 1
    assert not observed_roots[0].exists()
    assert (source / "alpha.py").read_text(encoding="utf-8") == "alpha\n"


def _write_json(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path
