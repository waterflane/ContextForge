"""Versioned, provider-independent discovery benchmark manifests."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from contextforge.core.validation import Sha256, validate_portable_relative_path
from contextforge.discovery.models import CompletenessWarning

BENCHMARK_SCHEMA_VERSION: Literal[1] = 1

NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]
ConfidenceValue = Annotated[
    float, Field(ge=0.0, le=1.0, allow_inf_nan=False, strict=True)
]
Rate = ConfidenceValue
NonNegativeFloat = Annotated[
    float, Field(ge=0.0, allow_inf_nan=False, strict=True)
]
RepositoryRelativePath = Annotated[str, AfterValidator(validate_portable_relative_path)]
ExpectedFacet = Annotated[str, Field(min_length=1, max_length=500)]
WarningCode = Annotated[
    str,
    Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
]


class BenchmarkModel(BaseModel):
    """Frozen, closed base for deterministic benchmark fixtures."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class BenchmarkMode(StrEnum):
    """Discovery strategies exercised by a benchmark task."""

    FRESH = "fresh"
    INDEXED = "indexed"
    HYBRID = "hybrid"


def _validate_text(value: str, *, label: str) -> str:
    if not value.strip() or value != value.strip() or "\x00" in value:
        raise ValueError(f"{label} must be canonical non-empty text")
    return value


def _validate_canonical(values: tuple[str, ...], *, label: str) -> tuple[str, ...]:
    if values != tuple(sorted(set(values))):
        raise ValueError(f"{label} must be sorted and unique")
    return values


def _reject_contextforge_state(
    values: tuple[str, ...], info: ValidationInfo
) -> tuple[str, ...]:
    if not info.data.get("allow_contextforge_state", False) and any(
        value.split("/", 1)[0].casefold() == ".contextforge" for value in values
    ):
        raise ValueError(
            "root .contextforge paths require allow_contextforge_state=true"
        )
    return values


class BenchmarkExpectations(BenchmarkModel):
    """Optional per-mode replacements for task expectations and limits."""

    allow_contextforge_state: bool = False
    repeat_count: PositiveInt | None = None
    include_paths: tuple[RepositoryRelativePath, ...] | None = None
    exclude_paths: tuple[RepositoryRelativePath, ...] | None = None
    required_files_all: tuple[RepositoryRelativePath, ...] | None = None
    required_files_any: tuple[
        Annotated[tuple[RepositoryRelativePath, ...], Field(min_length=1)], ...
    ] | None = None
    forbidden_files: tuple[RepositoryRelativePath, ...] | None = None
    expected_facets: tuple[ExpectedFacet, ...] | None = None
    max_selected_files: NonNegativeInt | None = None
    max_files_read: NonNegativeInt | None = None
    max_model_generations: NonNegativeInt | None = None
    max_provider_http_calls: NonNegativeInt | None = None
    allowed_warnings: tuple[WarningCode, ...] | None = None
    required_warnings: tuple[WarningCode, ...] | None = None

    @field_validator(
        "include_paths",
        "exclude_paths",
        "required_files_all",
        "forbidden_files",
    )
    @classmethod
    def validate_paths(
        cls, value: tuple[str, ...] | None, info: ValidationInfo
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        return _reject_contextforge_state(
            _validate_canonical(value, label=info.field_name or "paths"), info
        )

    @field_validator("required_files_any")
    @classmethod
    def validate_path_groups(
        cls, value: tuple[tuple[str, ...], ...] | None, info: ValidationInfo
    ) -> tuple[tuple[str, ...], ...] | None:
        if value is None:
            return None
        groups = tuple(
            _reject_contextforge_state(
                _validate_canonical(group, label="required_files_any group"), info
            )
            for group in value
        )
        if groups != tuple(sorted(set(groups))):
            raise ValueError("required_files_any groups must be sorted and unique")
        return groups

    @field_validator("expected_facets")
    @classmethod
    def validate_facets(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        facets = tuple(_validate_text(item, label="facet") for item in value)
        return _validate_canonical(facets, label="expected_facets")

    @field_validator("allowed_warnings", "required_warnings")
    @classmethod
    def validate_warnings(
        cls, value: tuple[str, ...] | None, info: ValidationInfo
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        warnings = tuple(_validate_text(item, label="warning code") for item in value)
        return _validate_canonical(warnings, label=info.field_name or "warnings")

    @model_validator(mode="after")
    def validate_path_sets(self) -> BenchmarkExpectations:
        included = set(self.include_paths or ())
        excluded = set(self.exclude_paths or ())
        required = set(self.required_files_all or ()) | {
            path for group in self.required_files_any or () for path in group
        }
        forbidden = set(self.forbidden_files or ())
        if included & excluded:
            raise ValueError("include_paths and exclude_paths must not overlap")
        if required & forbidden:
            raise ValueError("required and forbidden files must not overlap")
        return self


class BenchmarkModeOverrides(BenchmarkModel):
    """Fixed-key mode overrides with deterministic serialization order."""

    fresh: BenchmarkExpectations | None = None
    indexed: BenchmarkExpectations | None = None
    hybrid: BenchmarkExpectations | None = None


class BenchmarkTask(BenchmarkExpectations):
    """One repository-relative discovery benchmark definition."""

    task_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._-]+$")
    repository_path: RepositoryRelativePath
    task: str = Field(min_length=1, max_length=20_000)
    modes: tuple[BenchmarkMode, ...] = Field(min_length=1)
    repeat_count: PositiveInt = 1
    include_paths: tuple[RepositoryRelativePath, ...] = ()
    exclude_paths: tuple[RepositoryRelativePath, ...] = ()
    required_files_all: tuple[RepositoryRelativePath, ...] = ()
    required_files_any: tuple[
        Annotated[tuple[RepositoryRelativePath, ...], Field(min_length=1)], ...
    ] = ()
    forbidden_files: tuple[RepositoryRelativePath, ...] = ()
    expected_facets: tuple[ExpectedFacet, ...] = ()
    max_selected_files: NonNegativeInt
    max_files_read: NonNegativeInt
    max_model_generations: NonNegativeInt
    max_provider_http_calls: NonNegativeInt | None = None
    allowed_warnings: tuple[WarningCode, ...] = ()
    required_warnings: tuple[WarningCode, ...] = ()
    mode_overrides: BenchmarkModeOverrides = Field(
        default_factory=BenchmarkModeOverrides
    )

    @field_validator("repository_path")
    @classmethod
    def validate_repository_path(cls, value: str, info: ValidationInfo) -> str:
        _reject_contextforge_state((value,), info)
        return value

    @field_validator("task")
    @classmethod
    def validate_task_text(cls, value: str) -> str:
        return _validate_text(value, label="task")

    @field_validator("modes", mode="before")
    @classmethod
    def parse_modes(cls, value: object) -> object:
        if isinstance(value, (list, tuple)):
            return tuple(
                BenchmarkMode(item) if isinstance(item, str) else item for item in value
            )
        return value

    @field_validator("modes")
    @classmethod
    def validate_modes(
        cls, value: tuple[BenchmarkMode, ...]
    ) -> tuple[BenchmarkMode, ...]:
        order = {mode: index for index, mode in enumerate(BenchmarkMode)}
        if value != tuple(sorted(set(value), key=order.__getitem__)):
            raise ValueError("modes must be unique and ordered fresh, indexed, hybrid")
        return value

    @model_validator(mode="after")
    def validate_overrides(self) -> BenchmarkTask:
        configured = {
            mode
            for mode in BenchmarkMode
            if getattr(self.mode_overrides, mode.value) is not None
        }
        if configured - set(self.modes):
            raise ValueError("mode overrides may only target enabled modes")
        return self


class BenchmarkManifest(BenchmarkModel):
    """Complete benchmark suite validated before task execution begins."""

    schema_version: Literal[1]
    suite_name: str = Field(min_length=1, max_length=200)
    tasks: tuple[BenchmarkTask, ...] = Field(min_length=1)

    @field_validator("suite_name")
    @classmethod
    def validate_suite_name(cls, value: str) -> str:
        return _validate_text(value, label="suite_name")

    @field_validator("tasks")
    @classmethod
    def validate_tasks(
        cls, value: tuple[BenchmarkTask, ...]
    ) -> tuple[BenchmarkTask, ...]:
        identifiers = tuple(task.task_id for task in value)
        if identifiers != tuple(sorted(set(identifiers))):
            raise ValueError("tasks must have unique task_id values in sorted order")
        return value


class BenchmarkProviderCounters(BenchmarkModel):
    """Provider activity charged to one discovery run."""

    model_calls: NonNegativeInt = 0
    model_generations: NonNegativeInt = 0
    repair_generations: NonNegativeInt = 0
    auxiliary_provider_calls: NonNegativeInt = 0
    provider_discovery_calls: NonNegativeInt = 0
    provider_capability_calls: NonNegativeInt = 0
    transport_attempts: NonNegativeInt = 0
    total_provider_http_calls: NonNegativeInt = 0


class BenchmarkAnyFileExpectation(BenchmarkModel):
    """Evaluation of one group where any configured file is acceptable."""

    files: tuple[RepositoryRelativePath, ...]
    matched_files: tuple[RepositoryRelativePath, ...]
    passed: bool


class BenchmarkExpectationEvaluation(BenchmarkModel):
    """Machine-readable file and warning expectation evaluation."""

    required_files: tuple[RepositoryRelativePath, ...] = ()
    matched_required_files: tuple[RepositoryRelativePath, ...] = ()
    missing_required_files: tuple[RepositoryRelativePath, ...] = ()
    any_file_groups: tuple[BenchmarkAnyFileExpectation, ...] = ()
    forbidden_files: tuple[RepositoryRelativePath, ...] = ()
    selected_forbidden_files: tuple[RepositoryRelativePath, ...] = ()
    expected_facets: tuple[ExpectedFacet, ...] = ()
    covered_expected_facets: tuple[ExpectedFacet, ...] = ()
    unexpected_warnings: tuple[WarningCode, ...] = ()
    missing_required_warnings: tuple[WarningCode, ...] = ()
    passed: bool


class BenchmarkLimitEvaluation(BenchmarkModel):
    """One configured upper bound and its observed value."""

    limit: NonNegativeInt
    actual: NonNegativeInt
    passed: bool


class BenchmarkBudgetEvaluation(BenchmarkModel):
    """All configured benchmark limits evaluated after a discovery run."""

    selected_files: BenchmarkLimitEvaluation
    files_read: BenchmarkLimitEvaluation
    model_generations: BenchmarkLimitEvaluation
    provider_http_calls: BenchmarkLimitEvaluation | None = None
    passed: bool


class BenchmarkFailure(BenchmarkModel):
    """Typed task failure retained alongside successful benchmark results."""

    code: str = Field(min_length=1, max_length=200)
    error_type: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=2_000)


class BenchmarkRunResult(BenchmarkModel):
    """Canonical result for one task, mode, and repetition."""

    task_id: str
    repository_path: RepositoryRelativePath
    mode: BenchmarkMode
    repetition: PositiveInt
    status: Literal["complete", "failed", "cancelled"]
    passed: bool
    duration_ms: NonNegativeInt
    selected_files: tuple[RepositoryRelativePath, ...] = ()
    files_considered: NonNegativeInt = 0
    files_read: NonNegativeInt = 0
    source_snapshot_digest: Sha256 | None = None
    index_generation_id: Sha256 | None = None
    effective_configuration_digest: Sha256 | None = None
    provider_id: str
    model_id: str
    provider_counters: BenchmarkProviderCounters
    confidence: ConfidenceValue | None = None
    warnings: tuple[CompletenessWarning, ...] = ()
    provenance: Literal["model", "deterministic_fallback"] | None = None
    fallback_used: bool = False
    context_bytes: NonNegativeInt = 0
    expectations: BenchmarkExpectationEvaluation
    budgets: BenchmarkBudgetEvaluation
    failure: BenchmarkFailure | None = None


class BenchmarkIntegerRange(BenchmarkModel):
    """Inclusive observed range for an integer run metric."""

    minimum: NonNegativeInt
    maximum: NonNegativeInt


class BenchmarkConfidenceSummary(BenchmarkModel):
    """Aggregate confidence over complete comparable runs."""

    mean: ConfidenceValue
    minimum: ConfidenceValue
    maximum: ConfidenceValue
    spread: Rate


class BenchmarkDurationPercentiles(BenchmarkModel):
    """Deterministic nearest-rank duration percentiles."""

    p50_ms: NonNegativeInt
    p90_ms: NonNegativeInt
    p95_ms: NonNegativeInt


class BenchmarkDurationSummary(BenchmarkModel):
    """Duration summary over complete comparable runs."""

    mean_ms: NonNegativeFloat
    percentiles: BenchmarkDurationPercentiles | None = None


class BenchmarkPairwiseJaccard(BenchmarkModel):
    """Selected-file set similarity for one pair of repetitions."""

    first_repetition: PositiveInt
    second_repetition: PositiveInt
    similarity: Rate


class BenchmarkCohortMetrics(BenchmarkModel):
    """Quality and repeatability metrics for one strictly comparable cohort."""

    task_id: str
    repository_path: RepositoryRelativePath
    mode: BenchmarkMode
    source_snapshot_digest: Sha256 | None
    index_generation_id: Sha256 | None
    effective_configuration_digest: Sha256 | None
    total_run_count: PositiveInt
    complete_run_count: NonNegativeInt
    excluded_run_count: NonNegativeInt
    comparable_pair_count: NonNegativeInt
    stability_kind: Literal[
        "insufficient_data",
        "semantic_stability",
        "deterministic_fallback_repeatability",
    ]
    exact_selected_file_match_rate: Rate | None = None
    exact_ordered_match_rate: Rate | None = None
    required_file_recall: Rate | None = None
    forbidden_file_selection_rate: Rate | None = None
    expected_facet_coverage_rate: Rate | None = None
    pairwise_jaccard: tuple[BenchmarkPairwiseJaccard, ...] = ()
    mean_jaccard_similarity: Rate | None = None
    warning_stability: Rate | None = None
    fallback_rate: Rate | None = None
    confidence: BenchmarkConfidenceSummary | None = None
    duration: BenchmarkDurationSummary | None = None
    files_read_range: BenchmarkIntegerRange | None = None
    model_call_range: BenchmarkIntegerRange | None = None


class BenchmarkResult(BenchmarkModel):
    """Canonical, prose-free result DTO for one validated benchmark manifest."""

    schema_version: Literal[1] = BENCHMARK_SCHEMA_VERSION
    manifest_schema_version: Literal[1]
    suite_name: str
    runs: tuple[BenchmarkRunResult, ...]
    metrics: tuple[BenchmarkCohortMetrics, ...] = ()
    passed: bool


def load_benchmark_manifest(path: str | Path) -> BenchmarkManifest:
    """Read and validate an entire JSON manifest before any task can run."""

    return BenchmarkManifest.model_validate_json(Path(path).read_bytes())


__all__ = [
    "BENCHMARK_SCHEMA_VERSION",
    "BenchmarkAnyFileExpectation",
    "BenchmarkBudgetEvaluation",
    "BenchmarkCohortMetrics",
    "BenchmarkConfidenceSummary",
    "BenchmarkDurationPercentiles",
    "BenchmarkDurationSummary",
    "BenchmarkExpectations",
    "BenchmarkExpectationEvaluation",
    "BenchmarkFailure",
    "BenchmarkIntegerRange",
    "BenchmarkLimitEvaluation",
    "BenchmarkManifest",
    "BenchmarkMode",
    "BenchmarkModeOverrides",
    "BenchmarkProviderCounters",
    "BenchmarkPairwiseJaccard",
    "BenchmarkResult",
    "BenchmarkRunResult",
    "BenchmarkTask",
    "load_benchmark_manifest",
]
