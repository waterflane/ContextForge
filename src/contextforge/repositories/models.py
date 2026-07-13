"""Typed domain models for repository inventory snapshots."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from contextforge.repositories.files import normalize_relative_path

PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]


class ScanOptions(BaseModel):
    """Options that influence a future repository scan."""

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
    source: Literal["default", "gitignore", "contextforgeignore"]
    pattern: str | None = None

    model_config = ConfigDict(frozen=True)

    @field_validator("path")
    @classmethod
    def normalize_path(cls, path: str) -> str:
        """Store paths in the snapshot's deterministic portable form."""

        return normalize_relative_path(path)


class ScanSummary(BaseModel):
    """Aggregate counts for one repository snapshot."""

    file_count: NonNegativeInt
    ignored_count: NonNegativeInt
    total_size_bytes: NonNegativeInt
    languages: dict[str, NonNegativeInt] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True)


class ProjectSnapshot(BaseModel):
    """Immutable top-level result shape for a future repository scan."""

    root: Path
    files: tuple[ProjectFile, ...] = ()
    ignored_files: tuple[IgnoredFile, ...] = ()
    summary: ScanSummary

    model_config = ConfigDict(frozen=True)
