"""Strict records for repository-wide structural and semantic maps."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from contextforge.intelligence.models import (
    AnalyzerIdentity,
    IndexModel,
    Sha256,
    validate_portable_relative_path,
)
from contextforge.intelligence.semantic_models import (
    EvidenceReference,
    SemanticConfidence,
)

GLOBAL_MAP_SCHEMA_VERSION: Literal[1] = 1

RelationshipKind = Literal[
    "imports",
    "imported-by",
    "contains",
    "calls-name",
    "references",
    "source-test",
    "feature-membership",
    "entry-point-to-handler",
    "configuration-consumer",
    "semantic-related-to",
]
RelationshipProvenance = Literal["verified", "best-effort-structural", "model-inferred"]
DiagnosticProvenance = Literal[
    "verified", "best-effort-structural", "model-inferred", "operational"
]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,199}$")
_ShortText = Annotated[str, Field(min_length=1, max_length=2_000)]
_Question = Annotated[str, Field(min_length=1, max_length=1_000)]
_NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]


def _validate_identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("identifier must be bounded portable text")
    return value


def _validate_paths(value: tuple[str, ...]) -> tuple[str, ...]:
    result = tuple(validate_portable_relative_path(path) for path in value)
    if result != tuple(sorted(set(result))):
        raise ValueError("paths must be unique and canonical")
    return result


def _validate_identifiers(value: tuple[str, ...]) -> tuple[str, ...]:
    result = tuple(_validate_identifier(item) for item in value)
    if result != tuple(sorted(set(result))):
        raise ValueError("identifiers must be unique and canonical")
    return result


def _evidence_key(value: EvidenceReference) -> tuple[object, ...]:
    source_range = value.source_range
    position: tuple[int, int, int, int] = (
        (0, 0, 0, 0)
        if source_range is None
        else (
            source_range.start_line,
            source_range.start_column,
            source_range.end_line,
            source_range.end_column,
        )
    )
    return (value.path, *position, value.fact_ids)


class RepositoryRelationship(IndexModel):
    """One repository edge with an explicit epistemic classification."""

    relationship_id: str
    kind: RelationshipKind
    provenance: RelationshipProvenance
    source_file: str
    source_symbol_id: str | None = None
    target_file: str | None = None
    target_symbol_id: str | None = None
    target_name: str | None = None
    detection_method: str
    description: _ShortText | None = None
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=50)
    confidence: SemanticConfidence | None = None

    @field_validator("relationship_id", "detection_method")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return _validate_identifier(value)

    @field_validator("source_symbol_id", "target_symbol_id")
    @classmethod
    def validate_optional_identifier(cls, value: str | None) -> str | None:
        return value if value is None else _validate_identifier(value)

    @field_validator("source_file")
    @classmethod
    def validate_source_file(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @field_validator("target_file")
    @classmethod
    def validate_target_file(cls, value: str | None) -> str | None:
        return value if value is None else validate_portable_relative_path(value)

    @field_validator("target_name")
    @classmethod
    def validate_target_name(cls, value: str | None) -> str | None:
        if value is not None and (not value or len(value) > 500 or "\x00" in value):
            raise ValueError("relationship target name must be bounded text")
        return value

    @model_validator(mode="after")
    def validate_interpretation(self) -> RepositoryRelationship:
        if self.provenance == "model-inferred" and self.confidence is None:
            raise ValueError("model-inferred relationships require confidence")
        if self.provenance != "model-inferred" and (
            self.description is not None or self.confidence is not None
        ):
            raise ValueError("structural relationships cannot contain model prose")
        keys = tuple(_evidence_key(item) for item in self.evidence)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("relationship evidence must be unique and canonical")
        if self.target_file is None and self.target_symbol_id is not None:
            raise ValueError("target symbols require a target file")
        return self


class TestRelationship(IndexModel):
    """A source/test association, never a claim of runtime coverage."""

    relationship_id: str
    source_file: str
    test_file: str
    provenance: Literal["verified", "best-effort-structural"]
    detection_method: str
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=20)

    @field_validator("relationship_id", "detection_method")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return _validate_identifier(value)

    @field_validator("source_file", "test_file")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @model_validator(mode="after")
    def validate_distinct_and_canonical(self) -> TestRelationship:
        if self.source_file == self.test_file:
            raise ValueError("source and test paths must be distinct")
        keys = tuple(_evidence_key(item) for item in self.evidence)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("test evidence must be unique and canonical")
        return self


class RepositoryDiagnostic(IndexModel):
    """A bounded limitation, uncertainty, risk, or operational map diagnostic."""

    code: str
    message: _ShortText
    severity: Literal["info", "warning", "error"]
    provenance: DiagnosticProvenance
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=50)
    confidence: SemanticConfidence | None = None

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        return _validate_identifier(value)

    @model_validator(mode="after")
    def validate_provenance(self) -> RepositoryDiagnostic:
        if self.provenance == "model-inferred" and self.confidence is None:
            raise ValueError("model diagnostics require confidence")
        if self.provenance != "model-inferred" and self.confidence is not None:
            raise ValueError("non-model diagnostics cannot claim semantic confidence")
        keys = tuple(_evidence_key(item) for item in self.evidence)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("diagnostic evidence must be unique and canonical")
        return self


class CoverageSummary(IndexModel):
    """Explicit map input coverage; validity does not imply completeness."""

    total_files: _NonNegativeInt
    parsed_files: _NonNegativeInt
    semantically_analyzed_files: _NonNegativeInt
    total_symbols: _NonNegativeInt
    represented_files: _NonNegativeInt
    represented_symbols: _NonNegativeInt
    test_files: _NonNegativeInt
    partial: bool

    @model_validator(mode="after")
    def validate_counts(self) -> CoverageSummary:
        if self.parsed_files > self.total_files:
            raise ValueError("parsed coverage cannot exceed total files")
        if self.semantically_analyzed_files > self.total_files:
            raise ValueError("semantic coverage cannot exceed total files")
        if self.represented_files > self.total_files:
            raise ValueError("represented coverage cannot exceed total files")
        if self.represented_symbols > self.total_symbols:
            raise ValueError("represented symbols cannot exceed total symbols")
        if self.test_files > self.total_files:
            raise ValueError("test coverage cannot exceed total files")
        return self


class RepositoryOverview(IndexModel):
    """Deterministic repository-wide projection of the current CodeMaps."""

    schema_version: Literal[1] = GLOBAL_MAP_SCHEMA_VERSION
    record_kind: Literal["verified_repository_overview"] = (
        "verified_repository_overview"
    )
    source_snapshot_digest: Sha256
    facts_digest: Sha256
    repository_tree: tuple[str, ...]
    major_packages: tuple[str, ...]
    languages: dict[str, _NonNegativeInt]
    file_count: _NonNegativeInt
    parsed_file_count: _NonNegativeInt
    symbol_count: _NonNegativeInt
    test_file_count: _NonNegativeInt
    relationships: tuple[RepositoryRelationship, ...] = ()
    test_relationships: tuple[TestRelationship, ...] = ()
    diagnostics: tuple[RepositoryDiagnostic, ...] = ()

    @field_validator("repository_tree")
    @classmethod
    def validate_tree(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_paths(value)

    @field_validator("major_packages")
    @classmethod
    def validate_packages(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("major packages must be unique and canonical")
        if any(not item or len(item) > 500 or "\x00" in item for item in value):
            raise ValueError("major packages must be bounded text")
        return value

    @field_validator("languages")
    @classmethod
    def validate_languages(
        cls, value: dict[str, _NonNegativeInt]
    ) -> dict[str, _NonNegativeInt]:
        if tuple(value) != tuple(sorted(value)):
            raise ValueError("overview languages must use canonical key order")
        return value

    @model_validator(mode="after")
    def validate_derived_content(self) -> RepositoryOverview:
        if self.file_count != len(self.repository_tree):
            raise ValueError("overview file count does not match repository tree")
        if self.parsed_file_count > self.file_count:
            raise ValueError("parsed file count cannot exceed total files")
        if self.test_file_count > self.file_count:
            raise ValueError("test file count cannot exceed total files")
        if sum(self.languages.values()) > self.file_count:
            raise ValueError("language counts cannot exceed total files")
        relationship_ids = tuple(item.relationship_id for item in self.relationships)
        if relationship_ids != tuple(sorted(set(relationship_ids))):
            raise ValueError("overview relationships must be unique and canonical")
        test_ids = tuple(item.relationship_id for item in self.test_relationships)
        if test_ids != tuple(sorted(set(test_ids))):
            raise ValueError("test relationships must be unique and canonical")
        diagnostic_keys = tuple((item.code, item.message) for item in self.diagnostics)
        if diagnostic_keys != tuple(sorted(set(diagnostic_keys))):
            raise ValueError("overview diagnostics must be unique and canonical")
        return self


class _Interpretation(IndexModel):
    title: _ShortText
    description: _ShortText
    evidence: tuple[EvidenceReference, ...] = Field(default=(), max_length=100)
    confidence: SemanticConfidence
    unresolved_questions: tuple[_Question, ...] = Field(default=(), max_length=50)

    @field_validator("unresolved_questions")
    @classmethod
    def validate_questions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("unresolved questions must be unique and canonical")
        return value

    @model_validator(mode="after")
    def validate_evidence(self) -> _Interpretation:
        keys = tuple(_evidence_key(item) for item in self.evidence)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("interpretation evidence must be unique and canonical")
        return self


class ModuleRole(_Interpretation):
    """Model interpretation of a package/module architectural responsibility."""

    role_id: str
    role_kind: Literal[
        "domain-core",
        "application",
        "adapter",
        "cli",
        "api",
        "storage",
        "model-provider",
        "configuration",
        "testing",
        "support",
        "other",
    ]
    files: tuple[str, ...]
    symbols: tuple[str, ...] = ()

    @field_validator("role_id")
    @classmethod
    def validate_role_id(cls, value: str) -> str:
        return _validate_identifier(value)

    @field_validator("files")
    @classmethod
    def validate_files(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_paths(value)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_identifiers(value)


class DataFlow(_Interpretation):
    """Model interpretation of an important repository data/configuration flow."""

    flow_id: str
    flow_kind: Literal[
        "configuration",
        "request",
        "response",
        "persistence",
        "model-input",
        "model-output",
        "internal",
        "other",
    ]
    source: _ShortText
    target: _ShortText
    files: tuple[str, ...]
    symbols: tuple[str, ...] = ()

    @field_validator("flow_id")
    @classmethod
    def validate_flow_id(cls, value: str) -> str:
        return _validate_identifier(value)

    @field_validator("files")
    @classmethod
    def validate_files(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_paths(value)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_identifiers(value)


class EntryPoint(_Interpretation):
    """Model interpretation of a user, process, API, or library entry point."""

    entry_point_id: str
    entry_point_kind: Literal["cli", "api", "application", "library", "test", "other"]
    file: str
    symbol_id: str | None = None
    handler_file: str | None = None
    handler_symbol_id: str | None = None

    @field_validator("entry_point_id")
    @classmethod
    def validate_entry_id(cls, value: str) -> str:
        return _validate_identifier(value)

    @field_validator("file")
    @classmethod
    def validate_file(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @field_validator("handler_file")
    @classmethod
    def validate_handler_file(cls, value: str | None) -> str | None:
        return value if value is None else validate_portable_relative_path(value)

    @field_validator("symbol_id", "handler_symbol_id")
    @classmethod
    def validate_symbol(cls, value: str | None) -> str | None:
        return value if value is None else _validate_identifier(value)

    @model_validator(mode="after")
    def validate_handler(self) -> EntryPoint:
        if self.handler_symbol_id is not None and self.handler_file is None:
            raise ValueError("handler symbols require a handler file")
        return self


class ExternalBoundary(_Interpretation):
    """Model interpretation of storage, model, service, or process boundaries."""

    boundary_id: str
    boundary_kind: Literal[
        "storage",
        "model-provider",
        "external-service",
        "filesystem",
        "network",
        "database",
        "process",
        "configuration",
        "other",
    ]
    files: tuple[str, ...]
    symbols: tuple[str, ...] = ()

    @field_validator("boundary_id")
    @classmethod
    def validate_boundary_id(cls, value: str) -> str:
        return _validate_identifier(value)

    @field_validator("files")
    @classmethod
    def validate_files(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_paths(value)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_identifiers(value)


class FeatureArea(_Interpretation):
    """Behavior-based group spanning implementation and related tests."""

    feature_id: str
    participating_files: tuple[str, ...]
    participating_symbols: tuple[str, ...] = ()
    related_tests: tuple[str, ...] = ()

    @field_validator("feature_id")
    @classmethod
    def validate_feature_id(cls, value: str) -> str:
        return _validate_identifier(value)

    @field_validator("participating_files", "related_tests")
    @classmethod
    def validate_files(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_paths(value)

    @field_validator("participating_symbols")
    @classmethod
    def validate_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_identifiers(value)

    @model_validator(mode="after")
    def validate_membership(self) -> FeatureArea:
        if not self.participating_files and not self.participating_symbols:
            raise ValueError("feature areas require participating files or symbols")
        if not set(self.related_tests) <= set(self.participating_files):
            raise ValueError("related tests must also participate in the feature")
        return self


class ArchitectureMap(IndexModel):
    """Attributed repository architecture interpretation over verified facts."""

    schema_version: Literal[1] = GLOBAL_MAP_SCHEMA_VERSION
    record_kind: Literal["model_architecture_interpretation"] = (
        "model_architecture_interpretation"
    )
    source_snapshot_digest: Sha256
    facts_digest: Sha256
    source_interpretations_digest: Sha256
    analyzer: AnalyzerIdentity
    analysis_options_digest: Sha256
    module_roles: tuple[ModuleRole, ...] = ()
    data_flows: tuple[DataFlow, ...] = ()
    entry_points: tuple[EntryPoint, ...] = ()
    external_boundaries: tuple[ExternalBoundary, ...] = ()
    test_relationships: tuple[TestRelationship, ...] = ()
    relationships: tuple[RepositoryRelationship, ...] = ()
    diagnostics: tuple[RepositoryDiagnostic, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()
    confidence: SemanticConfidence
    coverage: CoverageSummary

    @model_validator(mode="after")
    def validate_canonical_content(self) -> ArchitectureMap:
        _require_ids(self.module_roles, "role_id", "module roles")
        _require_ids(self.data_flows, "flow_id", "data flows")
        _require_ids(self.entry_points, "entry_point_id", "entry points")
        _require_ids(self.external_boundaries, "boundary_id", "boundaries")
        _require_ids(self.test_relationships, "relationship_id", "test relationships")
        _require_ids(self.relationships, "relationship_id", "relationships")
        _require_diagnostics(self.diagnostics)
        _require_evidence(self.evidence)
        return self


class FeatureMap(IndexModel):
    """Attributed behavior-based feature grouping over verified facts."""

    schema_version: Literal[1] = GLOBAL_MAP_SCHEMA_VERSION
    record_kind: Literal["model_feature_interpretation"] = (
        "model_feature_interpretation"
    )
    source_snapshot_digest: Sha256
    facts_digest: Sha256
    source_interpretations_digest: Sha256
    analyzer: AnalyzerIdentity
    analysis_options_digest: Sha256
    feature_areas: tuple[FeatureArea, ...] = ()
    relationships: tuple[RepositoryRelationship, ...] = ()
    diagnostics: tuple[RepositoryDiagnostic, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()
    confidence: SemanticConfidence
    coverage: CoverageSummary

    @model_validator(mode="after")
    def validate_canonical_content(self) -> FeatureMap:
        _require_ids(self.feature_areas, "feature_id", "feature areas")
        _require_ids(self.relationships, "relationship_id", "relationships")
        _require_diagnostics(self.diagnostics)
        _require_evidence(self.evidence)
        return self


def _require_ids(values: tuple[object, ...], field: str, label: str) -> None:
    identifiers = tuple(getattr(item, field) for item in values)
    if identifiers != tuple(sorted(set(identifiers))):
        raise ValueError(f"{label} must be unique and canonical")


def _require_diagnostics(values: tuple[RepositoryDiagnostic, ...]) -> None:
    keys = tuple((item.code, item.message) for item in values)
    if keys != tuple(sorted(set(keys))):
        raise ValueError("diagnostics must be unique and canonical")


def _require_evidence(values: tuple[EvidenceReference, ...]) -> None:
    keys = tuple(_evidence_key(item) for item in values)
    if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
        raise ValueError("map evidence must be unique and canonical")


__all__ = [
    "GLOBAL_MAP_SCHEMA_VERSION",
    "ArchitectureMap",
    "CoverageSummary",
    "DataFlow",
    "EntryPoint",
    "ExternalBoundary",
    "FeatureArea",
    "FeatureMap",
    "ModuleRole",
    "RelationshipKind",
    "RelationshipProvenance",
    "RepositoryDiagnostic",
    "RepositoryOverview",
    "RepositoryRelationship",
    "TestRelationship",
]
