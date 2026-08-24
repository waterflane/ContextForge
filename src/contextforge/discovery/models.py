"""Closed typed contracts for bounded model-guided repository discovery."""

from __future__ import annotations

import hashlib
import math
import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contextforge.core.validation import Sha256, validate_portable_relative_path

DISCOVERY_SCHEMA_VERSION: Literal[1] = 1
DISCOVERY_APPLICATION_SCHEMA_VERSION: Literal[1] = 1

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
    max_preselected_candidates: int = Field(default=10, ge=0, le=100, strict=True)
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
    # Compatibility field: initial model requests initiated, including ones that
    # are rejected locally or fail before returning model content.
    model_calls: NonNegativeInt = 0
    model_generations: NonNegativeInt = 0
    repair_generations: NonNegativeInt = 0
    provider_discovery_calls: NonNegativeInt = 0
    provider_capability_calls: NonNegativeInt = 0
    transport_attempts: NonNegativeInt = 0
    total_provider_http_calls: NonNegativeInt = 0
    # Compatibility alias for total_provider_http_calls.
    provider_http_calls: NonNegativeInt = 0
    files_read: NonNegativeInt = 0
    source_bytes: NonNegativeInt = 0
    tool_result_bytes: NonNegativeInt = 0
    context_bytes: NonNegativeInt = 0
    context_files: NonNegativeInt = 0

    @model_validator(mode="before")
    @classmethod
    def normalize_provider_http_calls(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "total_provider_http_calls" not in normalized:
            normalized["total_provider_http_calls"] = normalized.get(
                "provider_http_calls", 0
            )
        if "provider_http_calls" not in normalized:
            normalized["provider_http_calls"] = normalized["total_provider_http_calls"]
        return normalized

    @model_validator(mode="after")
    def validate_provider_http_calls(self) -> DiscoveryBudgetUsage:
        if self.provider_http_calls != self.total_provider_http_calls:
            raise ValueError("provider_http_calls must equal total_provider_http_calls")
        return self


class DiscoveryRequest(DiscoveryModel):
    """Explicit task, mode, reviewer intent, and limits for discovery."""

    schema_version: Literal[1] = DISCOVERY_SCHEMA_VERSION
    task: str = Field(min_length=1, max_length=20_000)
    mode: DiscoveryMode = DiscoveryMode.HYBRID
    pinned_paths: tuple[str, ...] = ()
    excluded_paths: tuple[str, ...] = ()
    strict: bool = False
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
        if not value.strip() or "\x00" in value:
            raise ValueError("task must be bounded non-empty text")
        # The original user task is an immutable handoff input. Validation must
        # not normalize away leading/trailing whitespace or line structure.
        return value

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


class DiscoveryCandidateRecord(DiscoveryModel):
    """Compact ranked candidate serialized into one provider request."""

    candidate_id: str
    path: str
    language: str
    rank: PositiveInt
    score: float = Field(ge=0.0, allow_inf_nan=False)
    ranking_signals: tuple[str, ...] = Field(min_length=1, max_length=10)

    @field_validator("candidate_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("candidate_id must be a bounded portable identifier")
        return value

    @field_validator("path")
    @classmethod
    def validate_record_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)


class PreparedDiscoveryCandidate(DiscoveryModel):
    """Portable deterministic candidate offered to an external orchestrator."""

    candidate_id: str
    path: str
    language: str
    rank: PositiveInt
    score: float = Field(ge=0.0, allow_inf_nan=False)
    ranking_signals: tuple[str, ...] = Field(min_length=1, max_length=10)
    source_sha256: Sha256
    source_size_bytes: NonNegativeInt
    evidence_origin: Literal["snapshot", "fresh", "indexed", "hybrid"]
    structural_evidence: bool
    semantic_evidence: bool

    @field_validator("candidate_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("candidate_id must be a bounded portable identifier")
        return value

    @field_validator("path")
    @classmethod
    def validate_candidate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)


class DiscoveryCandidatePreparation(DiscoveryModel):
    """Immutable model-free discovery input pinned to repository truth."""

    schema_version: Literal[1] = DISCOVERY_APPLICATION_SCHEMA_VERSION
    preparation_id: Sha256
    task: str = Field(min_length=1, max_length=20_000)
    mode: DiscoveryMode
    pinned_paths: tuple[str, ...] = ()
    excluded_paths: tuple[str, ...] = ()
    strict: bool = False
    source_snapshot_digest: Sha256
    index_generation_id: Sha256 | None = None
    index_status: Literal["not_used", "unavailable", "current", "stale"]
    candidates: tuple[PreparedDiscoveryCandidate, ...]
    total_candidate_count: NonNegativeInt
    stale_index_paths: tuple[str, ...] = ()
    warnings: tuple[CompletenessWarning, ...] = ()
    budget: DiscoveryBudget
    budget_usage: DiscoveryBudgetUsage

    @field_validator("stale_index_paths")
    @classmethod
    def validate_stale_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        paths = tuple(validate_portable_relative_path(path) for path in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("stale index paths must be unique and canonical")
        return paths

    @field_validator("pinned_paths", "excluded_paths")
    @classmethod
    def validate_manual_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        paths = tuple(validate_portable_relative_path(path) for path in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("manual paths must be unique and canonical")
        return paths

    @model_validator(mode="after")
    def validate_candidates(self) -> DiscoveryCandidatePreparation:
        identifiers = tuple(item.candidate_id for item in self.candidates)
        ranks = tuple(item.rank for item in self.candidates)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("prepared candidate IDs must be unique")
        if ranks != tuple(sorted(ranks)):
            raise ValueError("prepared candidates must use canonical rank order")
        if self.total_candidate_count < len(self.candidates):
            raise ValueError("total candidate count cannot be below serialized count")
        return self


class DiscoveryExpansionRequest(DiscoveryModel):
    """One stateless read-only evidence operation over a preparation."""

    schema_version: Literal[1] = DISCOVERY_APPLICATION_SCHEMA_VERSION
    preparation_id: Sha256
    action_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    budget_usage: DiscoveryBudgetUsage = Field(default_factory=DiscoveryBudgetUsage)

    @field_validator("action_id", "tool_name")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("operation identifiers must be bounded and portable")
        return value


class DiscoveryExpansionResult(DiscoveryModel):
    """Immutable result of one deterministic discovery expansion."""

    schema_version: Literal[1] = DISCOVERY_APPLICATION_SCHEMA_VERSION
    preparation_id: Sha256
    observation: DiscoveryObservation
    budget_usage: DiscoveryBudgetUsage


class DiscoverySelectionItem(DiscoveryModel):
    """One prepared candidate selected by a caller, optionally by line range."""

    candidate_id: str
    ranges: tuple[DiscoveryLineRange, ...] = ()

    @field_validator("candidate_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("candidate_id must be a bounded portable identifier")
        return value

    @field_validator("ranges")
    @classmethod
    def validate_ranges(
        cls, value: tuple[DiscoveryLineRange, ...]
    ) -> tuple[DiscoveryLineRange, ...]:
        previous_end = 0
        for item in value:
            if item.start_line <= previous_end:
                raise ValueError("selection ranges must be sorted and disjoint")
            previous_end = item.end_line
        return value


class DiscoverySelection(DiscoveryModel):
    """Caller-owned selection over a specific deterministic preparation."""

    schema_version: Literal[1] = DISCOVERY_APPLICATION_SCHEMA_VERSION
    preparation_id: Sha256
    items: tuple[DiscoverySelectionItem, ...] = Field(min_length=1)
    budget_usage: DiscoveryBudgetUsage = Field(default_factory=DiscoveryBudgetUsage)

    @field_validator("items")
    @classmethod
    def validate_items(
        cls, value: tuple[DiscoverySelectionItem, ...]
    ) -> tuple[DiscoverySelectionItem, ...]:
        identifiers = tuple(item.candidate_id for item in value)
        if identifiers != tuple(sorted(set(identifiers))):
            raise ValueError("selection items must be unique and canonical")
        return value


class VerifiedContextBlock(DiscoveryModel):
    """One verified canonical source block."""

    start_line: PositiveInt | None = None
    end_line: PositiveInt | None = None
    text: str
    line_count: NonNegativeInt
    size_bytes: NonNegativeInt
    sha256: Sha256

    @model_validator(mode="after")
    def validate_bounds(self) -> VerifiedContextBlock:
        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("verified block bounds must both be set or absent")
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.end_line < self.start_line
        ):
            raise ValueError("verified block end must not precede start")
        if "\r" in self.text:
            raise ValueError("verified text must use canonical LF newlines")
        encoded = self.text.encode("utf-8")
        if self.size_bytes != len(encoded):
            raise ValueError("verified block size does not match UTF-8 content")
        if self.sha256 != hashlib.sha256(encoded).hexdigest():
            raise ValueError("verified block hash does not match content")
        line_count = (
            0
            if not self.text
            else self.text.count("\n") + (not self.text.endswith("\n"))
        )
        if self.line_count != line_count:
            raise ValueError("verified block line count does not match content")
        if (
            self.start_line is not None
            and self.end_line is not None
            and self.line_count != self.end_line - self.start_line + 1
        ):
            raise ValueError("verified block line count does not match range")
        return self


class VerifiedContextFile(DiscoveryModel):
    """One snapshot-owned file whose selected content was re-read and verified."""

    candidate_id: str
    path: str
    language: str | None = None
    source_size_bytes: NonNegativeInt
    source_sha256: Sha256
    source_line_count: NonNegativeInt
    blocks: tuple[VerifiedContextBlock, ...] = Field(min_length=1)
    included_line_count: NonNegativeInt
    included_content_bytes: NonNegativeInt

    @field_validator("path")
    @classmethod
    def validate_file_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @field_validator("candidate_id")
    @classmethod
    def validate_candidate_id(cls, value: str) -> str:
        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("candidate_id must be a bounded portable identifier")
        return value

    @model_validator(mode="after")
    def validate_content(self) -> VerifiedContextFile:
        if self.included_line_count != sum(item.line_count for item in self.blocks):
            raise ValueError("included line count does not match verified blocks")
        if self.included_content_bytes != sum(item.size_bytes for item in self.blocks):
            raise ValueError("included byte count does not match verified blocks")
        if self.included_content_bytes > self.source_size_bytes:
            raise ValueError("verified content cannot exceed source size")
        ranged = any(item.start_line is not None for item in self.blocks)
        if ranged and any(item.start_line is None for item in self.blocks):
            raise ValueError("verified file cannot mix full and ranged blocks")
        if not ranged and len(self.blocks) != 1:
            raise ValueError("verified full source requires exactly one block")
        return self


class VerifiedContext(DiscoveryModel):
    """All-or-nothing verified source content ready for deterministic packaging."""

    schema_version: Literal[1] = DISCOVERY_APPLICATION_SCHEMA_VERSION
    preparation_id: Sha256
    task: str = Field(min_length=1, max_length=20_000)
    mode: DiscoveryMode
    source_snapshot_digest: Sha256
    index_generation_id: Sha256 | None = None
    files: tuple[VerifiedContextFile, ...] = Field(min_length=1)
    budget_usage: DiscoveryBudgetUsage

    @model_validator(mode="after")
    def validate_files(self) -> VerifiedContext:
        identifiers = tuple(item.candidate_id for item in self.files)
        paths = tuple(item.path for item in self.files)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("verified candidate IDs must be unique")
        if paths != tuple(sorted(set(paths))):
            raise ValueError("verified paths must be unique and canonical")
        if self.budget_usage.context_files != len(self.files):
            raise ValueError("context file usage does not match verified files")
        if self.budget_usage.context_bytes != sum(
            item.included_content_bytes for item in self.files
        ):
            raise ValueError("context byte usage does not match verified files")
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

    @field_validator("actions")
    @classmethod
    def validate_unique_actions(
        cls, value: tuple[DiscoveryAction, ...]
    ) -> tuple[DiscoveryAction, ...]:
        identifiers = tuple(item.action_id for item in value)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("action IDs must be unique within one response")
        return value


class IndexedContextSelection(DiscoveryModel):
    """Compact model-facing selection returned by indexed context suggestion."""

    schema_version: Literal[1] = DISCOVERY_SCHEMA_VERSION
    candidate_ids: tuple[str, ...] = Field(min_length=1, max_length=10)
    summary: str = Field(min_length=1, max_length=2_000)

    @field_validator("candidate_ids")
    @classmethod
    def validate_candidate_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("candidate_ids must be unique")
        if any(not _IDENTIFIER.fullmatch(item) for item in value):
            raise ValueError("candidate_ids must contain portable identifiers")
        return value


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
    provenance: Literal["model", "deterministic_fallback"] = "model"

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
    "DISCOVERY_APPLICATION_SCHEMA_VERSION",
    "DISCOVERY_SCHEMA_VERSION",
    "CompletenessWarning",
    "DiscoveryAction",
    "DiscoveryActionBatch",
    "DiscoveryBudget",
    "DiscoveryBudgetUsage",
    "DiscoveryCandidate",
    "DiscoveryCandidatePreparation",
    "DiscoveryCandidateRecord",
    "DiscoveryExpansionRequest",
    "DiscoveryExpansionResult",
    "DiscoveryLineRange",
    "DiscoveryMode",
    "DiscoveryObservation",
    "DiscoveryRequest",
    "DiscoveryRunRecord",
    "DiscoverySelection",
    "DiscoverySelectionItem",
    "DiscoveryState",
    "IndexedContextSelection",
    "PreparedDiscoveryCandidate",
    "FinalContextSelection",
    "SelectionReason",
    "VerifiedContext",
    "VerifiedContextBlock",
    "VerifiedContextFile",
]
