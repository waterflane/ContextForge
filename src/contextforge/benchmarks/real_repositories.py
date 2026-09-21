"""Immutable contracts and opt-in execution for real-repository benchmarks.

The discovery benchmark runner deliberately operates on a caller supplied
working tree.  This module is the narrower contract used for reviewed external
repositories: a manifest pins the revision and expected evidence, while the
live harness clones that source into a disposable directory before evaluation.
It contains no retrieval policy; observations are supplied by the evaluated
pipeline so the contract cannot become a corpus-specific ranking rule.
"""

from __future__ import annotations

import inspect
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from contextforge.benchmarks.models import (
    BenchmarkExpectedAssertion,
    BenchmarkSourceRange,
)
from contextforge.core.validation import validate_portable_relative_path

REAL_REPOSITORY_BENCHMARK_SCHEMA_VERSION: Literal[1] = 1
RepositoryRelativePath = Annotated[str, AfterValidator(validate_portable_relative_path)]
Rate = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False, strict=True)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
GitRevision = Annotated[str, Field(pattern=r"^[0-9a-f]{40,64}$", strict=True)]


class RealBenchmarkModel(BaseModel):
    """Closed and immutable model base for an auditable benchmark contract."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RealBenchmarkTaskKind(StrEnum):
    BROAD = "broad"
    EXACT_SYMBOL = "exact_symbol"


class RealBenchmarkMode(StrEnum):
    DETERMINISTIC = "deterministic"
    PLANNED = "planned"


class RealBenchmarkTaskRole(RealBenchmarkModel):
    """An independently necessary evidence role for a task."""

    role_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = Field(min_length=1, max_length=500)
    required_paths: tuple[RepositoryRelativePath, ...] = Field(min_length=1)

    @field_validator("required_paths")
    @classmethod
    def canonical_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("role required_paths must be sorted and unique")
        return value


class RealBenchmarkRepository(RealBenchmarkModel):
    repository_id: str = Field(
        min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$"
    )
    revision: GitRevision


class RealBenchmarkTask(RealBenchmarkModel):
    """One reviewed task, expressed only as expected evidence and assertions."""

    task_id: str = Field(min_length=1, max_length=160, pattern=r"^[a-z0-9][a-z0-9-]*$")
    repository_id: str = Field(min_length=1, max_length=100)
    kind: RealBenchmarkTaskKind
    dataset_split: Literal["tuning", "holdout"]
    task: str = Field(min_length=1, max_length=4_000)
    required_files: tuple[RepositoryRelativePath, ...] = Field(
        min_length=1, max_length=5
    )
    relevant_top5: tuple[RepositoryRelativePath, ...] = Field(
        min_length=1, max_length=5
    )
    required_ranges: tuple[BenchmarkSourceRange, ...] = ()
    task_roles: tuple[RealBenchmarkTaskRole, ...] = Field(min_length=1)
    answer_assertions: tuple[BenchmarkExpectedAssertion, ...] = ()

    @field_validator("required_files", "relevant_top5")
    @classmethod
    def canonical_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("benchmark paths must be unique")
        return value

    @field_validator("required_ranges")
    @classmethod
    def canonical_ranges(
        cls, value: tuple[BenchmarkSourceRange, ...]
    ) -> tuple[BenchmarkSourceRange, ...]:
        keys = tuple((item.path, item.start_line, item.end_line) for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("required_ranges must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_evidence_contract(self) -> RealBenchmarkTask:
        required = set(self.required_files)
        role_paths = {path for role in self.task_roles for path in role.required_paths}
        if role_paths != required:
            raise ValueError("task roles must cover exactly the required files")
        if not set(self.relevant_top5) <= required:
            raise ValueError("relevant_top5 must be a subset of required_files")
        if not {item.path for item in self.required_ranges} <= required:
            raise ValueError("required_ranges must refer to required_files")
        assertion_ids = tuple(item.assertion_id for item in self.answer_assertions)
        if assertion_ids != tuple(sorted(set(assertion_ids))):
            raise ValueError("answer assertions must have canonical unique IDs")
        return self


class RealRepositoryBenchmarkManifest(RealBenchmarkModel):
    schema_version: Literal[1]
    suite_name: str = Field(min_length=1, max_length=200)
    repositories: tuple[RealBenchmarkRepository, ...] = Field(min_length=1)
    tasks: tuple[RealBenchmarkTask, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_repositories_and_tasks(self) -> RealRepositoryBenchmarkManifest:
        repository_ids = tuple(item.repository_id for item in self.repositories)
        if repository_ids != tuple(sorted(set(repository_ids))):
            raise ValueError("repositories must use sorted unique repository_id values")
        task_ids = tuple(item.task_id for item in self.tasks)
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("tasks must use unique task_id values")
        if {task.repository_id for task in self.tasks} - set(repository_ids):
            raise ValueError("task references an unknown repository")
        return self


class RealBenchmarkObservation(RealBenchmarkModel):
    """One pipeline measurement; the evaluator owns how it obtained it."""

    mode: RealBenchmarkMode
    retrieved_top5: tuple[RepositoryRelativePath, ...]
    materialized_files: tuple[RepositoryRelativePath, ...]
    materialized_ranges: tuple[BenchmarkSourceRange, ...] = ()
    capsule_tokens: NonNegativeInt
    ordinary_tokens: NonNegativeInt
    planner_calls: NonNegativeInt = 0
    planner_input_tokens: NonNegativeInt = 0
    planner_output_tokens: NonNegativeInt = 0
    latency_ms: NonNegativeInt = 0
    plan_sufficient: bool | None = None
    citation_validity: Rate = 0.0
    groundedness_majority: bool = False
    quality_not_lower_than_oracle: bool = False

    @field_validator("retrieved_top5", "materialized_files")
    @classmethod
    def canonical_observed_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(dict.fromkeys(value)):
            raise ValueError("observed paths must be unique in observed order")
        return value


class RealBenchmarkTaskReport(RealBenchmarkModel):
    task_id: str
    repository_id: str
    mode: RealBenchmarkMode
    status: Literal["complete", "skipped"]
    skip_reason: str | None = None
    required_file_recall_at_5: Rate | None = None
    precision_at_5: Rate | None = None
    materialized_required_file_recall: Rate | None = None
    range_recall: Rate | None = None
    capsule_tokens: NonNegativeInt = 0
    ordinary_tokens: NonNegativeInt = 0
    token_savings: float = Field(default=0.0, allow_inf_nan=False)
    valid_token_savings: float = Field(default=0.0, allow_inf_nan=False)
    planner_calls: NonNegativeInt = 0
    planner_input_tokens: NonNegativeInt = 0
    planner_output_tokens: NonNegativeInt = 0
    latency_ms: NonNegativeInt = 0
    plan_sufficient: bool | None = None
    citation_validity: Rate | None = None
    groundedness_majority: bool | None = None
    quality_not_lower_than_oracle: bool | None = None
    quality_gate_failed: bool

    @model_validator(mode="after")
    def validate_status(self) -> RealBenchmarkTaskReport:
        if self.status == "skipped":
            if self.skip_reason is None or not self.quality_gate_failed:
                raise ValueError(
                    "a skipped result needs a reason and failed quality gate"
                )
        elif self.skip_reason is not None:
            raise ValueError("a complete result cannot have a skip reason")
        return self


class RealBenchmarkAggregate(RealBenchmarkModel):
    mode: RealBenchmarkMode
    task_count: NonNegativeInt
    completed_task_count: NonNegativeInt
    skipped_task_count: NonNegativeInt
    required_file_recall_at_5: Rate | None = None
    precision_at_5: Rate | None = None
    materialized_required_file_recall: Rate | None = None
    range_recall: Rate | None = None
    capsule_tokens: NonNegativeInt = 0
    ordinary_tokens: NonNegativeInt = 0
    planner_calls: NonNegativeInt = 0
    planner_input_tokens: NonNegativeInt = 0
    planner_output_tokens: NonNegativeInt = 0
    mean_latency_ms: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    plan_sufficient_count: NonNegativeInt = 0
    planned_task_count: NonNegativeInt = 0
    citation_containment: Rate | None = None
    groundedness_majority_rate: Rate | None = None
    quality_gate_failed_count: NonNegativeInt = 0
    headline_token_savings: float = Field(default=0.0, allow_inf_nan=False)


class RealRepositoryBenchmarkReport(RealBenchmarkModel):
    schema_version: Literal[1] = REAL_REPOSITORY_BENCHMARK_SCHEMA_VERSION
    suite_name: str
    runs: tuple[RealBenchmarkTaskReport, ...]
    aggregates: tuple[RealBenchmarkAggregate, ...]
    passed: bool


def load_real_repository_benchmark_manifest(
    path: str | Path,
) -> RealRepositoryBenchmarkManifest:
    return RealRepositoryBenchmarkManifest.model_validate_json(Path(path).read_bytes())


def evaluate_real_repository_observation(
    task: RealBenchmarkTask, observation: RealBenchmarkObservation
) -> RealBenchmarkTaskReport:
    """Score an observation with fixed evidence gates, never task-specific rules."""

    required = set(task.required_files)
    retrieved = set(observation.retrieved_top5)
    materialized = set(observation.materialized_files)
    retrieval_recall = len(required & retrieved) / len(required)
    precision = len(set(task.relevant_top5) & retrieved) / max(
        len(observation.retrieved_top5), 1
    )
    materialized_recall = len(required & materialized) / len(required)
    measured_range_recall = _range_recall(
        task.required_ranges, observation.materialized_ranges
    )
    token_savings = (
        0.0
        if observation.ordinary_tokens == 0
        else (observation.ordinary_tokens - observation.capsule_tokens)
        / observation.ordinary_tokens
    )
    gate_failed = not (
        materialized_recall >= 0.90
        and measured_range_recall >= 0.85
        and observation.citation_validity == 1.0
        and observation.groundedness_majority
        and observation.quality_not_lower_than_oracle
    )
    return RealBenchmarkTaskReport(
        task_id=task.task_id,
        repository_id=task.repository_id,
        mode=observation.mode,
        status="complete",
        required_file_recall_at_5=retrieval_recall,
        precision_at_5=precision,
        materialized_required_file_recall=materialized_recall,
        range_recall=(
            measured_range_recall if task.required_ranges else None
        ),
        capsule_tokens=observation.capsule_tokens,
        ordinary_tokens=observation.ordinary_tokens,
        token_savings=token_savings,
        valid_token_savings=0.0 if gate_failed else token_savings,
        planner_calls=observation.planner_calls,
        planner_input_tokens=observation.planner_input_tokens,
        planner_output_tokens=observation.planner_output_tokens,
        latency_ms=observation.latency_ms,
        plan_sufficient=observation.plan_sufficient,
        citation_validity=observation.citation_validity,
        groundedness_majority=observation.groundedness_majority,
        quality_not_lower_than_oracle=observation.quality_not_lower_than_oracle,
        quality_gate_failed=gate_failed,
    )


def aggregate_real_repository_report(
    manifest: RealRepositoryBenchmarkManifest,
    runs: tuple[RealBenchmarkTaskReport, ...],
) -> RealRepositoryBenchmarkReport:
    """Aggregate separately by mode and exclude failed gates from savings."""

    expected = {task.task_id for task in manifest.tasks}
    if {run.task_id for run in runs} != expected or len(runs) != len(expected):
        raise ValueError("report must contain exactly one run for each manifest task")
    aggregates = tuple(
        _aggregate(mode, tuple(run for run in runs if run.mode == mode))
        for mode in RealBenchmarkMode
    )
    return RealRepositoryBenchmarkReport(
        suite_name=manifest.suite_name,
        runs=tuple(sorted(runs, key=lambda item: item.task_id)),
        aggregates=aggregates,
        passed=all(
            run.status == "complete" and not run.quality_gate_failed for run in runs
        ),
    )


def _aggregate(
    mode: RealBenchmarkMode, runs: tuple[RealBenchmarkTaskReport, ...]
) -> RealBenchmarkAggregate:
    complete = tuple(item for item in runs if item.status == "complete")

    def mean(attribute: str) -> float | None:
        values = tuple(getattr(item, attribute) for item in complete)
        usable = tuple(value for value in values if value is not None)
        return None if not usable else sum(usable) / len(usable)

    ordinary = sum(
        item.ordinary_tokens for item in complete if not item.quality_gate_failed
    )
    capsule = sum(
        item.capsule_tokens for item in complete if not item.quality_gate_failed
    )
    return RealBenchmarkAggregate(
        mode=mode,
        task_count=len(runs),
        completed_task_count=len(complete),
        skipped_task_count=len(runs) - len(complete),
        required_file_recall_at_5=mean("required_file_recall_at_5"),
        precision_at_5=mean("precision_at_5"),
        materialized_required_file_recall=mean("materialized_required_file_recall"),
        range_recall=mean("range_recall"),
        capsule_tokens=sum(item.capsule_tokens for item in complete),
        ordinary_tokens=sum(item.ordinary_tokens for item in complete),
        planner_calls=sum(item.planner_calls for item in complete),
        planner_input_tokens=sum(item.planner_input_tokens for item in complete),
        planner_output_tokens=sum(item.planner_output_tokens for item in complete),
        mean_latency_ms=mean("latency_ms"),
        plan_sufficient_count=sum(item.plan_sufficient is True for item in complete),
        planned_task_count=sum(item.plan_sufficient is not None for item in complete),
        citation_containment=mean("citation_validity"),
        groundedness_majority_rate=(
            None
            if not complete
            else sum(item.groundedness_majority is True for item in complete)
            / len(complete)
        ),
        quality_gate_failed_count=sum(item.quality_gate_failed for item in runs),
        headline_token_savings=(
            0.0 if ordinary == 0 else (ordinary - capsule) / ordinary
        ),
    )


def _range_recall(
    required: tuple[BenchmarkSourceRange, ...],
    selected: tuple[BenchmarkSourceRange, ...],
) -> float:
    if not required:
        return 1.0
    required_lines = sum(item.end_line - item.start_line + 1 for item in required)
    covered = 0
    for required_range in required:
        intervals = sorted(
            (
                max(required_range.start_line, item.start_line),
                min(required_range.end_line, item.end_line),
            )
            for item in selected
            if item.path == required_range.path
            and item.end_line >= required_range.start_line
            and item.start_line <= required_range.end_line
        )
        if not intervals:
            continue
        start, end = intervals[0]
        for next_start, next_end in intervals[1:]:
            if next_start <= end + 1:
                end = max(end, next_end)
            else:
                covered += end - start + 1
                start, end = next_start, next_end
        covered += end - start + 1
    return covered / required_lines


@contextmanager
def temporary_read_only_clone(source: str | Path, revision: str) -> Iterator[Path]:
    """Clone a pinned external repository and always remove the clone afterwards."""

    with tempfile.TemporaryDirectory(
        prefix="contextforge-real-benchmark-"
    ) as directory:
        target = Path(directory) / "repository"
        try:
            subprocess.run(
                ["git", "clone", "--quiet", "--no-local", str(source), str(target)],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", str(target), "checkout", "--quiet", "--detach", revision],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(
                "external repository clone or revision checkout failed"
            ) from exc
        yield target


RealRepositoryEvaluator = Callable[
    [Path, RealBenchmarkTask],
    RealBenchmarkObservation | Awaitable[RealBenchmarkObservation],
]


async def run_real_repository_benchmark(
    manifest: RealRepositoryBenchmarkManifest,
    sources: Mapping[str, str | Path],
    evaluator: RealRepositoryEvaluator,
) -> RealRepositoryBenchmarkReport:
    """Opt-in live harness. Missing/corrupt sources are skips and never passes."""

    reports: list[RealBenchmarkTaskReport] = []
    repositories = {item.repository_id: item for item in manifest.repositories}
    for task in manifest.tasks:
        repository = repositories[task.repository_id]
        source = sources.get(task.repository_id)
        if source is None:
            reports.append(
                _skipped(
                    task, RealBenchmarkMode.DETERMINISTIC, "external_source_missing"
                )
            )
            continue
        try:
            with temporary_read_only_clone(source, repository.revision) as clone:
                observed = evaluator(clone, task)
                observation = (
                    await observed if inspect.isawaitable(observed) else observed
                )
                reports.append(evaluate_real_repository_observation(task, observation))
        except (RuntimeError, OSError, ValueError):
            reports.append(
                _skipped(
                    task, RealBenchmarkMode.DETERMINISTIC, "external_source_unavailable"
                )
            )
    return aggregate_real_repository_report(manifest, tuple(reports))


def _skipped(
    task: RealBenchmarkTask, mode: RealBenchmarkMode, reason: str
) -> RealBenchmarkTaskReport:
    return RealBenchmarkTaskReport(
        task_id=task.task_id,
        repository_id=task.repository_id,
        mode=mode,
        status="skipped",
        skip_reason=reason,
        quality_gate_failed=True,
    )


__all__ = [
    "REAL_REPOSITORY_BENCHMARK_SCHEMA_VERSION",
    "RealBenchmarkMode",
    "RealBenchmarkObservation",
    "RealBenchmarkTask",
    "RealBenchmarkTaskKind",
    "RealBenchmarkTaskReport",
    "RealBenchmarkTaskRole",
    "RealRepositoryBenchmarkManifest",
    "RealRepositoryBenchmarkReport",
    "aggregate_real_repository_report",
    "evaluate_real_repository_observation",
    "load_real_repository_benchmark_manifest",
    "run_real_repository_benchmark",
    "temporary_read_only_clone",
]
