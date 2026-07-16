"""Closed schemas for attributed model interpretations of verified source."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from contextforge.intelligence.codemap import SourceRange, SymbolKind
from contextforge.intelligence.models import (
    AnalyzerIdentity,
    IndexModel,
    Sha256,
    validate_portable_relative_path,
)

SEMANTIC_SCHEMA_VERSION: Literal[1] = 1

ClaimText = Annotated[str, Field(min_length=1, max_length=2_000)]
RationaleText = Annotated[str, Field(min_length=1, max_length=1_000)]
DiagnosticText = Annotated[str, Field(min_length=1, max_length=1_000)]


class SemanticConfidence(IndexModel):
    """Finite model confidence kept separate from verified source facts."""

    value: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    rationale: RationaleText


class EvidenceReference(IndexModel):
    """A model citation bound to one verified source identity and optional range."""

    path: str
    source_sha256: Sha256
    source_range: SourceRange | None = None
    fact_ids: tuple[str, ...] = Field(default=(), max_length=20)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @field_validator("fact_ids")
    @classmethod
    def validate_fact_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("evidence fact IDs must be unique and canonical")
        if any(not item or len(item) > 200 or "\x00" in item for item in value):
            raise ValueError("evidence fact IDs must be bounded text")
        return value


class _ClaimDescription(IndexModel):
    """Provenance repeated on every model-generated claim by design."""

    claim: ClaimText
    confidence: SemanticConfidence
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=50)
    analyzer_prompt_version: str
    provider_id: str
    model_id: str
    source_sha256: Sha256

    @field_validator("analyzer_prompt_version", "provider_id", "model_id")
    @classmethod
    def validate_provenance_label(cls, value: str) -> str:
        if (
            not value
            or len(value) > 128
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("claim provenance must be bounded printable text")
        return value


class BehaviorDescription(_ClaimDescription):
    """Purpose, behavior, contract, exception, or uncertainty interpretation."""

    behavior_kind: Literal[
        "purpose",
        "architectural_role",
        "responsibility",
        "entry_point",
        "test_relationship",
        "exception",
        "precondition",
        "postcondition",
        "security",
        "uncertainty",
    ]


class SideEffectDescription(_ClaimDescription):
    """State or external effect interpretation with an explicit effect category."""

    effect_kind: Literal[
        "state_change",
        "external_call",
        "filesystem",
        "network",
        "database",
        "process",
        "logging",
        "other",
    ]


class DataFlowDescription(_ClaimDescription):
    """Semantic input, output, configuration, or other data-flow interpretation."""

    flow_kind: Literal["input", "output", "configuration", "internal"]


class SymbolSemanticAnalysis(IndexModel):
    """Attributed interpretation of one verified function, method, or class."""

    schema_version: Literal[1] = SEMANTIC_SCHEMA_VERSION
    record_kind: Literal["model_symbol_interpretation"] = "model_symbol_interpretation"
    symbol_id: str
    name: str
    qualified_name: str
    kind: SymbolKind
    declaration_range: SourceRange
    behavioral_purpose: BehaviorDescription | None = None
    inputs: tuple[DataFlowDescription, ...] = Field(default=(), max_length=20)
    outputs: tuple[DataFlowDescription, ...] = Field(default=(), max_length=20)
    state_changes: tuple[SideEffectDescription, ...] = Field(default=(), max_length=20)
    exceptions: tuple[BehaviorDescription, ...] = Field(default=(), max_length=20)
    external_calls: tuple[SideEffectDescription, ...] = Field(default=(), max_length=20)
    filesystem_effects: tuple[SideEffectDescription, ...] = Field(
        default=(), max_length=20
    )
    network_effects: tuple[SideEffectDescription, ...] = Field(
        default=(), max_length=20
    )
    database_effects: tuple[SideEffectDescription, ...] = Field(
        default=(), max_length=20
    )
    preconditions: tuple[BehaviorDescription, ...] = Field(default=(), max_length=20)
    postconditions: tuple[BehaviorDescription, ...] = Field(default=(), max_length=20)
    security_sensitive_behavior: tuple[BehaviorDescription, ...] = Field(
        default=(), max_length=20
    )
    uncertainty: tuple[BehaviorDescription, ...] = Field(default=(), max_length=20)

    @field_validator("symbol_id", "name", "qualified_name")
    @classmethod
    def validate_symbol_text(cls, value: str) -> str:
        if not value or len(value) > 500 or "\x00" in value:
            raise ValueError("symbol identity text must be bounded")
        return value


class FileSemanticAnalysis(IndexModel):
    """Complete, separately persisted model interpretation of one source file."""

    schema_version: Literal[1] = SEMANTIC_SCHEMA_VERSION
    record_kind: Literal["model_file_interpretation"] = "model_file_interpretation"
    path: str
    language: str | None
    source_sha256: Sha256
    source_size_bytes: int = Field(ge=0, strict=True)
    fact_record_sha256: Sha256
    codemap_analyzer: AnalyzerIdentity
    semantic_analyzer: AnalyzerIdentity
    analysis_options_digest: Sha256
    primary_purpose: BehaviorDescription | None = None
    architectural_roles: tuple[BehaviorDescription, ...] = Field(
        default=(), max_length=20
    )
    major_responsibilities: tuple[BehaviorDescription, ...] = Field(
        default=(), max_length=20
    )
    external_interactions: tuple[SideEffectDescription, ...] = Field(
        default=(), max_length=20
    )
    configuration_dependencies: tuple[DataFlowDescription, ...] = Field(
        default=(), max_length=20
    )
    major_side_effects: tuple[SideEffectDescription, ...] = Field(
        default=(), max_length=20
    )
    public_entry_points: tuple[BehaviorDescription, ...] = Field(
        default=(), max_length=20
    )
    test_relationships: tuple[BehaviorDescription, ...] = Field(
        default=(), max_length=20
    )
    uncertainty: tuple[BehaviorDescription, ...] = Field(default=(), max_length=20)
    symbols: tuple[SymbolSemanticAnalysis, ...] = Field(default=(), max_length=500)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str | None) -> str | None:
        if value is not None and (
            not value
            or len(value) > 100
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("language must be bounded printable text")
        return value

    @model_validator(mode="after")
    def validate_symbol_order(self) -> FileSemanticAnalysis:
        keys = tuple(
            (
                item.declaration_range.start_line,
                item.declaration_range.start_column,
                item.symbol_id,
            )
            for item in self.symbols
        )
        if keys != tuple(sorted(keys)) or len(
            {item.symbol_id for item in self.symbols}
        ) != len(self.symbols):
            raise ValueError("semantic symbols must be unique and canonical")
        return self


class AnalysisDiagnostic(IndexModel):
    """Bounded operational semantic-analysis diagnostic, never a source fact."""

    code: str
    message: DiagnosticText
    severity: Literal["info", "warning", "error"]
    path: str | None = None
    symbol_id: str | None = None

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if not value or len(value) > 100 or not value.isascii():
            raise ValueError("diagnostic code must be bounded ASCII")
        return value

    @field_validator("path")
    @classmethod
    def validate_optional_path(cls, value: str | None) -> str | None:
        return value if value is None else validate_portable_relative_path(value)


__all__ = [
    "SEMANTIC_SCHEMA_VERSION",
    "AnalysisDiagnostic",
    "BehaviorDescription",
    "DataFlowDescription",
    "EvidenceReference",
    "FileSemanticAnalysis",
    "SemanticConfidence",
    "SideEffectDescription",
    "SymbolSemanticAnalysis",
]
