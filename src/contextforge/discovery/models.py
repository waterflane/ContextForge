"""Closed typed contracts for bounded model-guided repository discovery."""

from __future__ import annotations

import math
import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contextforge.core.validation import Sha256, validate_portable_relative_path

DISCOVERY_SCHEMA_VERSION: Literal[1] = 1

NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]
ConfidenceValue = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,199}$")


class DiscoveryModel(BaseModel):
    """Frozen closed base used at every discovery trust boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class DiscoveryMode(StrEnum):
    """Available repository-discovery strategies."""

    INDEXED = "indexed"
    FRESH = "fresh"
    HYBRID = "hybrid"


class DiscoveryBudget(DiscoveryModel):
    """Caller-selected hard limits for one discovery run."""

    max_steps: int = Field(default=40, ge=1, le=100, strict=True)
    max_model_calls: int = Field(default=20, ge=1, le=100, strict=True)
    max_files_read: int = Field(default=100, ge=1, le=1_000, strict=True)
    max_source_bytes: int = Field(
        default=2 * 1024 * 1024, ge=1, le=16 * 1024 * 1024, strict=True
    )
    max_tool_result_bytes: int = Field(
        default=2 * 1024 * 1024, ge=1, le=16 * 1024 * 1024, strict=True
    )
    max_context_bytes: int = Field(
        default=1024 * 1024, ge=1, le=10 * 1024 * 1024, strict=True
    )
    max_context_files: int = Field(default=100, ge=1, le=1_000, strict=True)
    timeout_seconds: float = Field(default=300.0, gt=0.0, le=900.0)
    repeated_action_warning: int = Field(default=3, ge=2, le=4, strict=True)
    repeated_action_limit: int = Field(default=5, ge=3, le=10, strict=True)

    @field_validator("timeout_seconds")
    @classmethod
    def validate_timeout(cls, value: float) -> float:
        if isinstance(value, bool) or not math.isfinite(value):
            raise ValueError("timeout_seconds must be finite")
        return value

    @model_validator(mode="after")
    def validate_loop_limits(self) -> DiscoveryBudget:
        if self.repeated_action_warning >= self.repeated_action_limit:
            raise ValueError("repeated action warning must precede the hard limit")
        return self


class DiscoveryBudgetUsage(DiscoveryModel):
    """Authoritative byte and operation charges accumulated by the engine."""

    steps: NonNegativeInt = 0
    model_calls: NonNegativeInt = 0
    files_read: NonNegativeInt = 0
    source_bytes: NonNegativeInt = 0
    tool_result_bytes: NonNegativeInt = 0
    context_bytes: NonNegativeInt = 0
    context_files: NonNegativeInt = 0


class DiscoveryRequest(DiscoveryModel):
    """Explicit task, mode, reviewer intent, and limits for discovery."""

    schema_version: Literal[1] = DISCOVERY_SCHEMA_VERSION
    task: str = Field(min_length=1, max_length=20_000)
    mode: DiscoveryMode = DiscoveryMode.HYBRID
    pinned_paths: tuple[str, ...] = ()
    excluded_paths: tuple[str, ...] = ()
    budget: DiscoveryBudget = Field(default_factory=DiscoveryBudget)

    @field_validator("mode", mode="before")
    @classmethod
    def validate_mode(cls, value: object) -> object:
        if isinstance(value, str):
            return DiscoveryMode(value)
        return value

    @field_validator("task")
    @classmethod
    def validate_task(cls, value: str) -> str:
        task = value.strip()
        if not task or "\x00" in task:
            raise ValueError("task must be bounded non-empty text")
        return task

    @field_validator("pinned_paths", "excluded_paths")
    @classmethod
    def validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        paths = tuple(validate_portable_relative_path(path) for path in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("manual paths must be unique and canonical")
        return paths

    @model_validator(mode="after")
    def validate_manual_precedence(self) -> DiscoveryRequest:
        if set(self.pinned_paths) & set(self.excluded_paths):
            raise ValueError("a path cannot be both manually pinned and excluded")
        return self


class DiscoveryLineRange(DiscoveryModel):
    """One one-based inclusive selected line range."""

    start_line: PositiveInt
    end_line: PositiveInt

    @model_validator(mode="after")
    def validate_order(self) -> DiscoveryLineRange:
        if self.end_line < self.start_line:
            raise ValueError("line range end must not precede its start")
        return self


class SelectionReason(DiscoveryModel):
    """Reviewable explanation and provenance for one selected item."""

    summary: str = Field(min_length=1, max_length=2_000)
    discovery_source: str = Field(min_length=1, max_length=200)
    evidence: tuple[str, ...] = Field(default=(), max_length=50)

    @field_validator("summary", "discovery_source")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("selection reason contains NUL")
        return value

    @field_validator("evidence")
    @classmethod
    def validate_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or len(item) > 500 or "\x00" in item for item in value):
            raise ValueError("selection evidence must be bounded text")
        return value


class DiscoveryCandidate(DiscoveryModel):
    """One reviewable source, structural, semantic, test, or diff selection."""

    candidate_id: str
    kind: Literal[
        "full_file",
        "line_ranges",
        "codemap",
        "architecture_note",
        "git_diff",
        "related_test",
    ]
    path: str | None = None
    ranges: tuple[DiscoveryLineRange, ...] = ()
    reason: SelectionReason
    confidence: ConfidenceValue | None = None
    source_sha256: Sha256 | None = None
    manually_pinned: bool = False
    model_selected: bool = False
    added_by_completeness: bool = False

    @field_validator("candidate_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("candidate_id must be a bounded portable identifier")
        return value

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        return value if value is None else validate_portable_relative_path(value)

    @model_validator(mode="after")
    def validate_shape(self) -> DiscoveryCandidate:
        path_required = self.kind not in {"architecture_note", "git_diff"}
        if path_required != (self.path is not None):
            raise ValueError("candidate path does not match candidate kind")
        if self.kind == "line_ranges":
            if not self.ranges:
                raise ValueError("line-range candidates require ranges")
            previous_end = 0
            for item in self.ranges:
                if item.start_line <= previous_end:
                    raise ValueError("candidate ranges must be sorted and disjoint")
                previous_end = item.end_line
        elif self.ranges:
            raise ValueError("only line-range candidates may contain ranges")
        if self.path is None and self.source_sha256 is not None:
            raise ValueError("non-file candidates cannot claim a source hash")
        return self


class CompletenessWarning(DiscoveryModel):
    """Advisory missing-context or static-analysis limitation."""

    code: str
    message: str = Field(min_length=1, max_length=2_000)
    severity: Literal["info", "warning"] = "warning"
    path: str | None = None
    related_paths: tuple[str, ...] = ()
    confidence: ConfidenceValue | None = None

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("warning code must be a bounded portable identifier")
        return value

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        return value if value is None else validate_portable_relative_path(value)

    @field_validator("related_paths")
    @classmethod
    def validate_related_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        paths = tuple(validate_portable_relative_path(path) for path in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("related warning paths must be unique and canonical")
        return paths


class DiscoveryAction(DiscoveryModel):
    """One strictly parsed model request for an engine-owned operation."""

    schema_version: Literal[1] = DISCOVERY_SCHEMA_VERSION
    action_id: str
    kind: Literal["call_tool", "finalize"]
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)

    @field_validator("action_id")
    @classmethod
    def validate_action_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("action_id must be a bounded portable identifier")
        return value

    @field_validator("tool_name")
    @classmethod
    def validate_tool_name(cls, value: str | None) -> str | None:
        if value is not None and not _IDENTIFIER.fullmatch(value):
            raise ValueError("tool_name must be a bounded portable identifier")
        return value

    @model_validator(mode="after")
    def validate_action_shape(self) -> DiscoveryAction:
        if self.kind == "call_tool" and self.tool_name is None:
            raise ValueError("call_tool actions require tool_name")
        if self.kind == "finalize" and self.tool_name is not None:
            raise ValueError("finalize actions cannot name a tool")
        return self


class DiscoveryActionBatch(DiscoveryModel):
    """Bounded set of actions returned by one provider call."""

    schema_version: Literal[1] = DISCOVERY_SCHEMA_VERSION
    actions: tuple[DiscoveryAction, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_unique_actions(self) -> DiscoveryActionBatch:
        identifiers = tuple(item.action_id for item in self.actions)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("action IDs must be unique within one response")
        return self


class DiscoveryObservation(DiscoveryModel):
    """Structured bounded result of validating and executing one action."""

    schema_version: Literal[1] = DISCOVERY_SCHEMA_VERSION
    step: PositiveInt
    action_id: str
    tool_name: str
    ok: bool
    code: str
    data: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False
    result_bytes: NonNegativeInt = 0
    made_progress: bool = False


class DiscoveryState(DiscoveryModel):
    """Reviewable immutable snapshot of an in-progress discovery session."""

    schema_version: Literal[1] = DISCOVERY_SCHEMA_VERSION
    task: str
    mode: DiscoveryMode
    candidates: tuple[DiscoveryCandidate, ...] = ()
    selected: tuple[DiscoveryCandidate, ...] = ()
    observations: tuple[DiscoveryObservation, ...] = ()
    warnings: tuple[CompletenessWarning, ...] = ()
    unknowns: tuple[str, ...] = ()
    budget_usage: DiscoveryBudgetUsage = Field(default_factory=DiscoveryBudgetUsage)


class FinalContextSelection(DiscoveryModel):
    """Successful review artifact; source text is still materialized separately."""

    schema_version: Literal[1] = DISCOVERY_SCHEMA_VERSION
    task: str
    mode: DiscoveryMode
    source_snapshot_digest: Sha256
    index_generation_id: Sha256 | None = None
    selected: tuple[DiscoveryCandidate, ...]
    summary: str = Field(min_length=1, max_length=10_000)
    unknowns: tuple[str, ...] = Field(default=(), max_length=100)
    completeness_warnings: tuple[CompletenessWarning, ...] = ()
    confidence: ConfidenceValue
    budget_usage: DiscoveryBudgetUsage
    run_id: str

    @model_validator(mode="after")
    def validate_selection(self) -> FinalContextSelection:
        if not self.selected:
            raise ValueError("final discovery selection must not be empty")
        identifiers = tuple(item.candidate_id for item in self.selected)
        if identifiers != tuple(sorted(set(identifiers))):
            raise ValueError("final candidates must be unique and canonical")
        return self


class DiscoveryRunRecord(DiscoveryModel):
    """Deterministic audit record for a complete, failed, or cancelled run."""

    schema_version: Literal[1] = DISCOVERY_SCHEMA_VERSION
    run_id: str
    status: Literal["complete", "failed", "cancelled"]
    request: DiscoveryRequest
    source_snapshot_digest: Sha256
    index_generation_id: Sha256 | None = None
    observations: tuple[DiscoveryObservation, ...] = ()
    warnings: tuple[CompletenessWarning, ...] = ()
    budget_usage: DiscoveryBudgetUsage = Field(default_factory=DiscoveryBudgetUsage)
    final_selection: FinalContextSelection | None = None
    failure_code: str | None = None
    failure_message: str | None = None

    @model_validator(mode="after")
    def validate_terminal_state(self) -> DiscoveryRunRecord:
        if self.status == "complete":
            if self.final_selection is None or self.failure_code is not None:
                raise ValueError("complete runs require only a final selection")
        elif self.final_selection is not None:
            raise ValueError("failed or cancelled runs cannot expose a final selection")
        elif self.failure_code is None or self.failure_message is None:
            raise ValueError("unsuccessful runs require a typed failure")
        return self


__all__ = [
    "DISCOVERY_SCHEMA_VERSION",
    "CompletenessWarning",
    "DiscoveryAction",
    "DiscoveryActionBatch",
    "DiscoveryBudget",
    "DiscoveryBudgetUsage",
    "DiscoveryCandidate",
    "DiscoveryLineRange",
    "DiscoveryMode",
    "DiscoveryObservation",
    "DiscoveryRequest",
    "DiscoveryRunRecord",
    "DiscoveryState",
    "FinalContextSelection",
    "SelectionReason",
]
