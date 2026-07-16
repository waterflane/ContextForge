"""Strict deterministic models for repository-intelligence index storage."""

from __future__ import annotations

import re
from collections import Counter
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

INDEX_SCHEMA_VERSION: Literal[1] = 1
MANIFEST_SCHEMA_VERSION: Literal[1] = 1
RECORD_SCHEMA_VERSION: Literal[1] = 1

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]
RecordStatus = Literal["complete", "failed", "skipped", "unsupported"]
SemanticStatus = Literal[
    "pending",
    "analyzing",
    "complete",
    "failed",
    "stale",
    "skipped",
    "disabled",
]

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,127}$")


class IndexModel(BaseModel):
    """Closed frozen base for persisted index models."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class SchemaVersionMetadata(IndexModel):
    """Independent schema versions that participate in invalidation."""

    index_schema_version: PositiveInt = INDEX_SCHEMA_VERSION
    manifest_schema_version: PositiveInt = MANIFEST_SCHEMA_VERSION
    record_schema_version: PositiveInt = RECORD_SCHEMA_VERSION


class ModelIdentity(IndexModel):
    """Provider/model provenance without credentials or transport settings."""

    provider_id: str
    model_id: str

    @field_validator("provider_id", "model_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        """Keep provenance bounded, printable, and deterministic."""

        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("model identity must be a bounded portable identifier")
        return value


class AnalyzerIdentity(IndexModel):
    """Every deterministic and semantic input that identifies an analysis."""

    analyzer_id: str
    analyzer_version: str
    analysis_prompt_version: str
    response_schema_version: PositiveInt
    model_identity: ModelIdentity | None = None

    @field_validator("analyzer_id", "analyzer_version", "analysis_prompt_version")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        """Reject volatile, multiline, or unbounded analyzer labels."""

        if not _IDENTIFIER.fullmatch(value):
            raise ValueError("analyzer identity must be a bounded portable identifier")
        return value


class IndexedFileState(IndexModel):
    """The last successfully published state for one repository file."""

    path: str
    source_sha256: Sha256
    source_size_bytes: NonNegativeInt
    language: str | None = None
    analyzer: AnalyzerIdentity
    record_location: str | None = None
    record_sha256: Sha256 | None = None
    record_status: RecordStatus
    interpretation_record_location: str | None = None
    interpretation_record_sha256: Sha256 | None = None
    semantic_status: SemanticStatus = "disabled"

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """Require a canonical repository-relative source path."""

        return validate_portable_relative_path(value)

    @field_validator("record_location")
    @classmethod
    def validate_record_location(cls, value: str | None) -> str | None:
        """Keep record references inside an immutable generation."""

        if value is None:
            return None
        location = validate_portable_relative_path(value)
        if not location.startswith("files/"):
            raise ValueError("file record locations must be beneath files/")
        return location

    @field_validator("interpretation_record_location")
    @classmethod
    def validate_interpretation_record_location(cls, value: str | None) -> str | None:
        """Keep interpretation references separate from structural facts."""

        if value is None:
            return None
        location = validate_portable_relative_path(value)
        if not location.startswith("files/") or not location.endswith(
            ".interpretation.json"
        ):
            raise ValueError(
                "interpretation record locations must be files/*.interpretation.json"
            )
        return location

    @field_validator("language")
    @classmethod
    def validate_language(cls, value: str | None) -> str | None:
        """Reject non-displayable language labels."""

        if value is not None and (
            not value
            or len(value) > 100
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("language must be a bounded printable label")
        return value

    @model_validator(mode="after")
    def validate_record_reference(self) -> IndexedFileState:
        """A published record is either fully addressed or explicitly absent."""

        has_location = self.record_location is not None
        has_digest = self.record_sha256 is not None
        if has_location != has_digest:
            raise ValueError("record_location and record_sha256 must be set together")
        if self.record_status in {"complete", "unsupported"} and not has_location:
            raise ValueError("successful records require a location and digest")
        if self.record_status in {"failed", "skipped"} and has_location:
            raise ValueError("failed or skipped records cannot reference a record")
        has_interpretation_location = self.interpretation_record_location is not None
        has_interpretation_digest = self.interpretation_record_sha256 is not None
        if has_interpretation_location != has_interpretation_digest:
            raise ValueError(
                "interpretation record location and digest must be set together"
            )
        if self.semantic_status == "complete" and not has_interpretation_location:
            raise ValueError("complete semantic records require a location and digest")
        if self.semantic_status != "complete" and has_interpretation_location:
            raise ValueError(
                "non-complete semantic records cannot reference an interpretation"
            )
        return self


class IndexBuildState(IndexModel):
    """Canonical last-successful build inputs, intentionally without timestamps."""

    status: Literal["complete"] = "complete"
    source_snapshot_digest: Sha256
    index_config_digest: Sha256
    build_options_digest: Sha256
    facts_digest: Sha256
    interpretations_digest: Sha256 | None = None
    previous_generation_id: Sha256 | None = None


class IndexStatistics(IndexModel):
    """Deterministic counts derived from the canonical file states."""

    file_count: NonNegativeInt
    complete_record_count: NonNegativeInt
    failed_record_count: NonNegativeInt
    skipped_record_count: NonNegativeInt
    unsupported_record_count: NonNegativeInt
    total_source_bytes: NonNegativeInt
    languages: dict[str, NonNegativeInt] = Field(default_factory=dict)

    @field_validator("languages")
    @classmethod
    def validate_language_order(
        cls, value: dict[str, NonNegativeInt]
    ) -> dict[str, NonNegativeInt]:
        """Require byte-stable language-key order before JSON serialization."""

        if tuple(value) != tuple(sorted(value)):
            raise ValueError("language counts must use canonical key order")
        return value


class IndexManifest(IndexModel):
    """Complete immutable generation manifest."""

    schema_version: Literal[1] = MANIFEST_SCHEMA_VERSION
    schema_versions: SchemaVersionMetadata = Field(
        default_factory=SchemaVersionMetadata
    )
    generation_id: Sha256
    build: IndexBuildState
    files: tuple[IndexedFileState, ...] = ()
    statistics: IndexStatistics
    structural_analyzers: tuple[AnalyzerIdentity, ...] = ()
    semantic_analyzers: tuple[AnalyzerIdentity, ...] = ()

    @model_validator(mode="after")
    def validate_canonical_content(self) -> IndexManifest:
        """Reject noncanonical ordering and inconsistent derived counts."""

        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("manifest files must have unique canonical paths")
        for analyzers in (self.structural_analyzers, self.semantic_analyzers):
            keys = tuple(analyzer_identity_key(item) for item in analyzers)
            if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
                raise ValueError("manifest analyzers must be unique and canonical")
        if self.statistics != calculate_index_statistics(self.files):
            raise ValueError("index statistics do not match manifest files")
        return self


class ActiveIndexPointer(IndexModel):
    """Small atomic root document that selects one immutable generation."""

    schema_version: Literal[1] = INDEX_SCHEMA_VERSION
    generation_id: Sha256
    generation_manifest: str
    source_snapshot_digest: Sha256

    @field_validator("generation_manifest")
    @classmethod
    def validate_generation_manifest(cls, value: str) -> str:
        """Pin the exact safe location implied by the generation identifier."""

        return validate_portable_relative_path(value)

    @model_validator(mode="after")
    def validate_manifest_location(self) -> ActiveIndexPointer:
        """Prevent pointers from selecting arbitrary generated paths."""

        expected = f"generations/{self.generation_id}/manifest.json"
        if self.generation_manifest != expected:
            raise ValueError("generation_manifest does not match generation_id")
        return self


class IndexStatus(IndexModel):
    """Read-only comparison of a current snapshot with the active manifest."""

    initialized: bool
    active_generation_id: Sha256 | None = None
    added_files: tuple[str, ...] = ()
    changed_files: tuple[str, ...] = ()
    unchanged_files: tuple[str, ...] = ()
    deleted_files: tuple[str, ...] = ()
    stale_analysis: tuple[str, ...] = ()

    @field_validator(
        "added_files",
        "changed_files",
        "unchanged_files",
        "deleted_files",
        "stale_analysis",
    )
    @classmethod
    def validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Status paths use the same canonical portable policy as manifests."""

        validated = tuple(validate_portable_relative_path(path) for path in value)
        if validated != tuple(sorted(validated)) or len(validated) != len(
            set(validated)
        ):
            raise ValueError("status paths must be unique and canonical")
        return validated


def calculate_index_statistics(
    files: tuple[IndexedFileState, ...],
) -> IndexStatistics:
    """Calculate canonical manifest statistics from file states."""

    statuses = Counter(item.record_status for item in files)
    languages = Counter(item.language for item in files if item.language is not None)
    return IndexStatistics(
        file_count=len(files),
        complete_record_count=statuses["complete"],
        failed_record_count=statuses["failed"],
        skipped_record_count=statuses["skipped"],
        unsupported_record_count=statuses["unsupported"],
        total_source_bytes=sum(item.source_size_bytes for item in files),
        languages=dict(sorted(languages.items())),
    )


def analyzer_identity_key(identity: AnalyzerIdentity) -> tuple[str, ...]:
    """Return the stable ordering key for an analyzer identity."""

    model = identity.model_identity
    return (
        identity.analyzer_id,
        identity.analyzer_version,
        identity.analysis_prompt_version,
        str(identity.response_schema_version),
        model.provider_id if model is not None else "",
        model.model_id if model is not None else "",
    )


def validate_portable_relative_path(value: str) -> str:
    """Validate one already-canonical portable path without filesystem access."""

    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("path must be a canonical portable relative path")
    if value.startswith("/") or _WINDOWS_DRIVE.match(value):
        raise ValueError("path must be a canonical portable relative path")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("path must be a canonical portable relative path")
    return value
