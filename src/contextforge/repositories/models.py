"""Typed domain models for repository inventory snapshots."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from contextforge.repositories.files import normalize_relative_path

PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class ScanOptions(BaseModel):
    """Options that influence a repository scan."""

    max_file_size_bytes: PositiveInt = 1_000_000
    respect_gitignore: bool = True
    respect_contextforgeignore: bool = True

    model_config = ConfigDict(frozen=True)


class ProjectFile(BaseModel):
    """Useful metadata for one repository-relative file."""

    path: str
    size_bytes: NonNegativeInt
    language: str | None = None
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    is_text: bool

    model_config = ConfigDict(frozen=True)

    @field_validator("path")
    @classmethod
    def normalize_path(cls, path: str) -> str:
        """Store paths in the snapshot's deterministic portable form."""

        return normalize_relative_path(path)


class IgnoredFile(BaseModel):
    """A repository-relative path excluded by an ignore-rule source."""

    path: str
    source: Literal["protected", "default", "gitignore", "contextforgeignore"]
    pattern: str | None = None

    model_config = ConfigDict(frozen=True)

    @field_validator("path")
    @classmethod
    def normalize_path(cls, path: str) -> str:
        """Store paths in the snapshot's deterministic portable form."""

        return normalize_relative_path(path)


class SkippedFile(BaseModel):
    """A non-ignored repository entry excluded from the useful inventory."""

    path: str
    reason: Literal["binary", "too_large", "unreadable", "symlink", "unsupported"]
    detail: str | None = None

    model_config = ConfigDict(frozen=True)

    @field_validator("path")
    @classmethod
    def normalize_path(cls, path: str) -> str:
        """Store paths in the snapshot's deterministic portable form."""

        return normalize_relative_path(path)


class ScanSummary(BaseModel):
    """Aggregate counts for one repository snapshot.

    ``discovered_count`` counts file-like entries reached by traversal; contents
    below protected or unreadable directories cannot be discovered. Ordinary
    ignored regular files are counted by ``ignored_count``, while pruned VCS
    metadata roots are counted separately by ``protected_count``.
    """

    file_count: NonNegativeInt
    ignored_count: NonNegativeInt
    total_size_bytes: NonNegativeInt
    languages: dict[str, NonNegativeInt] = Field(default_factory=dict)
    discovered_count: NonNegativeInt = 0
    protected_count: NonNegativeInt = 0
    binary_count: NonNegativeInt = 0
    oversized_count: NonNegativeInt = 0
    failed_count: NonNegativeInt = 0
    symlink_count: NonNegativeInt = 0
    unsupported_count: NonNegativeInt = 0
    skipped_count: NonNegativeInt = 0

    model_config = ConfigDict(frozen=True)


class ProjectSnapshot(BaseModel):
    """Immutable top-level result shape for a repository scan."""

    root: Path
    files: tuple[ProjectFile, ...] = ()
    ignored_files: tuple[IgnoredFile, ...] = ()
    skipped_files: tuple[SkippedFile, ...] = ()
    summary: ScanSummary

    model_config = ConfigDict(frozen=True)
