"""Versioned review, refinement, handoff, and compiled-prompt models."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contextforge.context import ContextPackage, ContextSelection
from contextforge.core.validation import Sha256, validate_portable_relative_path
from contextforge.discovery import (
    CompletenessWarning,
    DiscoveryMode,
    DiscoveryRunRecord,
    SelectionReason,
)
from contextforge.git import GitDiffContext
from contextforge.intelligence import FileCodeMap, serialize_code_map
from contextforge.prompts import PromptPackage

HANDOFF_SCHEMA_VERSION: Literal[1] = 1
TASK_REFINEMENT_PROMPT_VERSION = "task-refinement-1"
PROMPT_COMPILER_VERSION = "context-handoff-1"

NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]
ConfidenceValue = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]


class HandoffModel(BaseModel):
    """Closed immutable base for reviewable portable artifacts."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class TaskRefinementResponse(HandoffModel):
    """Strict model response before operational provenance is attached."""

    schema_version: Literal[1] = HANDOFF_SCHEMA_VERSION
    refined_task: str | None = Field(default=None, max_length=20_000)
    acceptance_criteria: tuple[str, ...] = Field(default=(), max_length=100)
    open_questions: tuple[str, ...] = Field(default=(), max_length=100)
    likely_affected_areas: tuple[str, ...] = Field(default=(), max_length=100)
    preserved_user_constraints: tuple[str, ...] = Field(default=(), max_length=100)

    @field_validator(
        "acceptance_criteria",
        "open_questions",
        "likely_affected_areas",
        "preserved_user_constraints",
    )
    @classmethod
    def validate_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not item.strip() or len(item) > 2_000 or "\x00" in item for item in value
        ):
            raise ValueError("task refinement items must be bounded non-empty text")
        if len(value) != len(set(value)):
            raise ValueError("task refinement items must be unique")
        return value

    @field_validator("refined_task")
    @classmethod
    def validate_refined_task(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or "\x00" in value):
            raise ValueError("refined_task must be bounded non-empty text")
        return value


class TaskRefinement(HandoffModel):
    """Clearly labelled generated clarification with complete provenance."""

    schema_version: Literal[1] = HANDOFF_SCHEMA_VERSION
    generated: Literal[True] = True
    provider: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=200)
    prompt_version: str = TASK_REFINEMENT_PROMPT_VERSION
    source_package_identity: Sha256
    refined_task: str | None = Field(default=None, max_length=20_000)
    acceptance_criteria: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    likely_affected_areas: tuple[str, ...] = ()
    preserved_user_constraints: tuple[str, ...] = ()

    @field_validator("provider", "model", "prompt_version")
    @classmethod
    def validate_provenance_text(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("task-refinement provenance must be printable text")
        return value

    @model_validator(mode="after")
    def validate_generated_content(self) -> TaskRefinement:
        TaskRefinementResponse(
            refined_task=self.refined_task,
            acceptance_criteria=self.acceptance_criteria,
            open_questions=self.open_questions,
            likely_affected_areas=self.likely_affected_areas,
            preserved_user_constraints=self.preserved_user_constraints,
        )
        return self

    @classmethod
    def from_response(
        cls,
        response: TaskRefinementResponse,
        *,
        provider: str,
        model: str,
        source_package_identity: str,
    ) -> TaskRefinement:
        """Attach provider provenance to one already validated response."""

        return cls(
            provider=provider,
            model=model,
            source_package_identity=source_package_identity,
            refined_task=response.refined_task,
            acceptance_criteria=response.acceptance_criteria,
            open_questions=response.open_questions,
            likely_affected_areas=response.likely_affected_areas,
            preserved_user_constraints=response.preserved_user_constraints,
        )


class ReviewLineRange(HandoffModel):
    """One one-based inclusive reviewed range."""

    start_line: PositiveInt
    end_line: PositiveInt

    @model_validator(mode="after")
    def validate_order(self) -> ReviewLineRange:
        if self.end_line < self.start_line:
            raise ValueError("review range end must not precede its start")
        return self


class ReviewSelectionItem(HandoffModel):
    """One selected, reduced, CodeMap-only, or explicitly omitted item."""

    path: str
    source_sha256: Sha256
    representation: Literal["full_source", "source_ranges", "codemap_only", "omitted"]
    ranges: tuple[ReviewLineRange, ...] = ()
    reason: SelectionReason
    confidence: ConfidenceValue | None = None
    estimated_source_bytes: NonNegativeInt
    estimated_included_bytes: NonNegativeInt
    pinned: bool = False
    automatic: bool = True
    category: Literal["primary", "supporting", "test", "structural"]

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @model_validator(mode="after")
    def validate_representation(self) -> ReviewSelectionItem:
        has_ranges = bool(self.ranges)
        if (self.representation == "source_ranges") != has_ranges:
            raise ValueError(
                "source_ranges representation must contain reviewed ranges"
            )
        if (
            self.representation in {"codemap_only", "omitted"}
            and self.estimated_included_bytes
        ):
            raise ValueError("non-source representations cannot include source bytes")
        return self


class SelectionOverride(HandoffModel):
    """Reviewer-controlled use of the existing manual selector contract."""

    schema_version: Literal[1] = HANDOFF_SCHEMA_VERSION
    selection: ContextSelection = Field(default_factory=ContextSelection)
    replace_discovered: bool = False


class HandoffBudgetLimits(HandoffModel):
    """Separate byte limits for handoff material categories."""

    max_source_bytes: int = Field(
        default=1024 * 1024, ge=1, le=10 * 1024 * 1024, strict=True
    )
    max_codemap_bytes: int = Field(
        default=256 * 1024, ge=1, le=4 * 1024 * 1024, strict=True
    )
    max_architecture_bytes: int = Field(
        default=128 * 1024, ge=1, le=2 * 1024 * 1024, strict=True
    )
    max_git_diff_bytes: int = Field(
        default=256 * 1024, ge=1, le=1024 * 1024, strict=True
    )
    max_prompt_instruction_bytes: int = Field(
        default=64 * 1024, ge=1, le=1024 * 1024, strict=True
    )
    max_total_prompt_bytes: int = Field(
        default=2 * 1024 * 1024, ge=1, le=16 * 1024 * 1024, strict=True
    )
    max_files: int = Field(default=100, ge=1, le=1_000, strict=True)


class HandoffBudgetUsage(HandoffModel):
    """Authoritative byte accounting without claiming exact model tokens."""

    source_content_bytes: NonNegativeInt = 0
    codemap_bytes: NonNegativeInt = 0
    architecture_note_bytes: NonNegativeInt = 0
    git_diff_bytes: NonNegativeInt = 0
    prompt_instruction_bytes: NonNegativeInt = 0
    total_prompt_bytes: NonNegativeInt = 0


class DiscoveryProvenance(HandoffModel):
    """Portable identity of the discovery result materialized into a package."""

    mode: DiscoveryMode
    run_id: str = Field(min_length=1, max_length=200)
    source_snapshot_digest: Sha256
    index_generation_id: Sha256 | None = None
    summary: str = Field(min_length=1, max_length=10_000)
    confidence: ConfidenceValue


class ContextSelectionReview(HandoffModel):
    """Review checkpoint that must be approved before source materialization."""

    schema_version: Literal[1] = HANDOFF_SCHEMA_VERSION
    original_task: str = Field(min_length=1, max_length=20_000)
    refined_task: TaskRefinement | None = None
    acceptance_criteria: tuple[str, ...] = ()
    discovery: DiscoveryProvenance
    selected_items: tuple[ReviewSelectionItem, ...]
    warnings: tuple[CompletenessWarning, ...] = ()
    budget_limits: HandoffBudgetLimits
    estimated_budget_usage: HandoffBudgetUsage
    override: SelectionOverride | None = None

    @field_validator("original_task")
    @classmethod
    def validate_task(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("original task must be preserved as bounded text")
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_criteria(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not item.strip() or len(item) > 2_000 or "\x00" in item for item in value
        ):
            raise ValueError("acceptance criteria must be bounded non-empty text")
        return value

    @model_validator(mode="after")
    def validate_items(self) -> ContextSelectionReview:
        paths = tuple(item.path for item in self.selected_items)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("review items must have unique canonical paths")
        if not any(
            item.representation in {"full_source", "source_ranges"}
            for item in self.selected_items
        ):
            raise ValueError("review must retain at least one source item")
        return self


class HandoffCodeMap(HandoffModel):
    """Selected current CodeMap with deterministic size and identity metadata."""

    path: str
    reason: str = Field(min_length=1, max_length=2_000)
    size_bytes: NonNegativeInt
    sha256: Sha256
    code_map: FileCodeMap

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @model_validator(mode="after")
    def validate_code_map(self) -> HandoffCodeMap:
        serialized = serialize_code_map(self.code_map)
        if self.path != self.code_map.path:
            raise ValueError("CodeMap path does not match handoff path")
        if self.size_bytes != len(serialized):
            raise ValueError("CodeMap size does not match canonical content")
        if self.sha256 != hashlib.sha256(serialized).hexdigest():
            raise ValueError("CodeMap digest does not match canonical content")
        return self


class ArchitectureNote(HandoffModel):
    """Bounded generated architecture or feature interpretation."""

    note_id: str = Field(min_length=1, max_length=200)
    kind: Literal[
        "module_role", "data_flow", "entry_point", "boundary", "feature", "diagnostic"
    ]
    title: str = Field(min_length=1, max_length=2_000)
    description: str = Field(min_length=1, max_length=2_000)
    paths: tuple[str, ...] = ()
    confidence: ConfidenceValue | None = None
    generated: Literal[True] = True

    @field_validator("note_id", "title", "description")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("architecture-note text must not contain NUL")
        return value

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        paths = tuple(validate_portable_relative_path(path) for path in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("architecture-note paths must be unique and canonical")
        return paths


class TaskHandoff(HandoffModel):
    """Complete portable discovery-to-package artifact; no agent execution."""

    schema_version: Literal[1] = HANDOFF_SCHEMA_VERSION
    original_task: str = Field(min_length=1, max_length=20_000)
    refined_task: TaskRefinement | None = None
    acceptance_criteria: tuple[str, ...] = ()
    review: ContextSelectionReview
    context_package: ContextPackage
    source_package_identity: Sha256
    codemaps: tuple[HandoffCodeMap, ...] = ()
    architecture_notes: tuple[ArchitectureNote, ...] = ()
    git_diff: GitDiffContext | None = None
    known_constraints: tuple[str, ...] = ()
    completeness_warnings: tuple[CompletenessWarning, ...] = ()
    expected_response_format: str = Field(min_length=1, max_length=10_000)
    budget_usage: HandoffBudgetUsage

    @field_validator("known_constraints")
    @classmethod
    def validate_constraints(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not item.strip() or len(item) > 2_000 or "\x00" in item for item in value
        ):
            raise ValueError("known constraints must be bounded non-empty text")
        if len(value) != len(set(value)):
            raise ValueError("known constraints must be unique")
        return value

    @field_validator("expected_response_format")
    @classmethod
    def validate_expected_format(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("expected response format must be bounded non-empty text")
        return value

    @model_validator(mode="after")
    def validate_handoff(self) -> TaskHandoff:
        if self.original_task != self.review.original_task:
            raise ValueError("handoff original task must match the approved review")
        if self.refined_task != self.review.refined_task:
            raise ValueError("handoff refinement must match the approved review")
        if self.acceptance_criteria != self.review.acceptance_criteria:
            raise ValueError("handoff criteria must match the approved review")
        if self.source_package_identity != calculate_context_package_identity(
            self.context_package
        ):
            raise ValueError("source package identity does not match ContextPackage")
        package_paths = tuple(item.path for item in self.context_package.files)
        reviewed_paths = tuple(
            item.path
            for item in self.review.selected_items
            if item.representation in {"full_source", "source_ranges"}
        )
        if package_paths != reviewed_paths:
            raise ValueError("ContextPackage files must match reviewed source items")
        codemap_paths = tuple(item.path for item in self.codemaps)
        if codemap_paths != tuple(sorted(set(codemap_paths))):
            raise ValueError("handoff CodeMaps must be unique and canonical")
        note_ids = tuple(item.note_id for item in self.architecture_notes)
        if note_ids != tuple(sorted(set(note_ids))):
            raise ValueError("architecture notes must be unique and canonical")
        return self


class CompiledPromptMetadata(HandoffModel):
    """Deterministic identity and size accounting for a compiled prompt."""

    schema_version: Literal[1] = HANDOFF_SCHEMA_VERSION
    prompt_version: str = PROMPT_COMPILER_VERSION
    source_handoff_identity: Sha256
    prompt_sha256: Sha256
    prompt_bytes: NonNegativeInt
    budget_usage: HandoffBudgetUsage
    token_count: None = None
    token_count_note: Literal["not-calculated-no-tokenizer"] = (
        "not-calculated-no-tokenizer"
    )


class CompiledPrompt(HandoffModel):
    """Prompt text plus deterministic metadata; never an execution request."""

    schema_version: Literal[1] = HANDOFF_SCHEMA_VERSION
    prompt: PromptPackage
    metadata: CompiledPromptMetadata


class DiscoveryHandoffResult(HandoffModel):
    """One complete discovery audit paired with its verified task handoff."""

    schema_version: Literal[1] = HANDOFF_SCHEMA_VERSION
    discovery_run: DiscoveryRunRecord
    handoff: TaskHandoff

    @model_validator(mode="after")
    def validate_link(self) -> DiscoveryHandoffResult:
        final = self.discovery_run.final_selection
        if self.discovery_run.status != "complete" or final is None:
            raise ValueError(
                "discovery handoff results require a complete discovery run"
            )
        if final.run_id != self.handoff.review.discovery.run_id:
            raise ValueError("discovery run does not match handoff provenance")
        return self


def canonical_json_bytes(value: object) -> bytes:
    """Return stable UTF-8 JSON bytes for portable handoff identities."""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def calculate_context_package_identity(package: ContextPackage) -> str:
    """Hash the canonical schema-1 package without local machine state."""

    return hashlib.sha256(
        canonical_json_bytes(package.model_dump(mode="json"))
    ).hexdigest()


def calculate_handoff_identity(handoff: TaskHandoff) -> str:
    """Hash the complete uncompiled handoff artifact."""

    return hashlib.sha256(
        canonical_json_bytes(handoff.model_dump(mode="json"))
    ).hexdigest()


__all__ = [
    "HANDOFF_SCHEMA_VERSION",
    "PROMPT_COMPILER_VERSION",
    "TASK_REFINEMENT_PROMPT_VERSION",
    "ArchitectureNote",
    "CompiledPrompt",
    "CompiledPromptMetadata",
    "ContextSelectionReview",
    "DiscoveryHandoffResult",
    "DiscoveryProvenance",
    "HandoffBudgetLimits",
    "HandoffBudgetUsage",
    "HandoffCodeMap",
    "ReviewLineRange",
    "ReviewSelectionItem",
    "SelectionOverride",
    "TaskHandoff",
    "TaskRefinement",
    "TaskRefinementResponse",
    "calculate_context_package_identity",
    "calculate_handoff_identity",
    "canonical_json_bytes",
]
