"""Immutable canonical context-package domain models."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from contextforge.context.tree import ProjectTree

NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SelectionMode = Literal["full", "ranges"]

DEFAULT_CONTEXT_TASK = "Context package"
CONTEXT_PACKAGE_SCHEMA_VERSION: Literal[1] = 1

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class ContextBlock(BaseModel):
    """One canonical full-file or inclusive line-range content block."""

    start_line: Annotated[int, Field(gt=0, strict=True)] | None
    end_line: Annotated[int, Field(gt=0, strict=True)] | None
    text: str
    line_count: NonNegativeInt
    size_bytes: NonNegativeInt
    sha256: Sha256

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="after")
    def validate_content(self) -> ContextBlock:
        """Reject inconsistent bounds and canonical-content metadata."""

        if (self.start_line is None) != (self.end_line is None):
            raise ValueError("start_line and end_line must either both be set or null")
        if self.start_line is not None and self.end_line is not None:
            if self.end_line < self.start_line:
                raise ValueError("end_line must not be before start_line")
            expected_range_lines = self.end_line - self.start_line + 1
            if self.line_count != expected_range_lines:
                raise ValueError("line_count does not match the inclusive line range")
        if "\r" in self.text:
            raise ValueError("block text must use canonical LF newlines")

        encoded = self.text.encode("utf-8")
        if self.size_bytes != len(encoded):
            raise ValueError("size_bytes does not match UTF-8 block content")
        if self.sha256 != hashlib.sha256(encoded).hexdigest():
            raise ValueError("sha256 does not match UTF-8 block content")
        if self.line_count != canonical_line_count(self.text):
            raise ValueError("line_count does not match block text")
        return self


class ContextFile(BaseModel):
    """One selected source file and its canonical included content."""

    path: str
    language: str | None = None
    source_size_bytes: NonNegativeInt
    source_sha256: Sha256
    source_line_count: NonNegativeInt
    selection: SelectionMode
    blocks: tuple[ContextBlock, ...]
    included_line_count: NonNegativeInt
    included_content_bytes: NonNegativeInt

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        """Require a portable repository-relative file path."""

        return validate_portable_package_path(path)

    @field_validator("language")
    @classmethod
    def validate_language(cls, language: str | None) -> str | None:
        """Reject labels that cannot be displayed as one safe metadata value."""

        if language is not None:
            _validate_language_label(language)
        return language

    @model_validator(mode="after")
    def validate_blocks(self) -> ContextFile:
        """Reject duplicate, noncanonical, or contradictory block shapes."""

        if self.selection == "full":
            if len(self.blocks) != 1:
                raise ValueError("a full file must contain exactly one block")
            block = self.blocks[0]
            if block.start_line is not None or block.end_line is not None:
                raise ValueError("a full-file block must have null line bounds")
            if block.line_count != self.source_line_count:
                raise ValueError("full-file line count must match source_line_count")
        else:
            if not self.blocks:
                raise ValueError("a ranged file must contain at least one block")
            previous_end = 0
            for block in self.blocks:
                if block.start_line is None or block.end_line is None:
                    raise ValueError("ranged blocks must have inclusive line bounds")
                if block.start_line <= previous_end + 1 and previous_end:
                    raise ValueError(
                        "ranged blocks must be sorted, disjoint, and non-adjacent"
                    )
                if block.end_line > self.source_line_count:
                    raise ValueError("ranged block exceeds source_line_count")
                previous_end = block.end_line

        if self.included_line_count != sum(block.line_count for block in self.blocks):
            raise ValueError("included_line_count does not match blocks")
        if self.included_content_bytes != sum(
            block.size_bytes for block in self.blocks
        ):
            raise ValueError("included_content_bytes does not match blocks")
        if self.included_content_bytes > self.source_size_bytes:
            raise ValueError("included content cannot exceed source_size_bytes")
        return self


# ContextItem is the user-facing concept; ContextFile retains the approved plan's
# more precise name and future JSON-schema terminology.
ContextItem = ContextFile


class ContextProject(BaseModel):
    """Portable repository inventory metadata with no local root path."""

    selectable_file_count: NonNegativeInt
    selectable_directory_count: NonNegativeInt
    selectable_source_bytes: NonNegativeInt
    languages: dict[str, NonNegativeInt] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("languages")
    @classmethod
    def validate_languages(
        cls, languages: dict[str, NonNegativeInt]
    ) -> dict[str, NonNegativeInt]:
        """Require stable, meaningful language-count metadata."""

        _validate_language_counts(languages)
        return languages

    @model_validator(mode="after")
    def validate_language_total(self) -> ContextProject:
        """Language counts describe a subset of the selectable file inventory."""

        if sum(self.languages.values()) > self.selectable_file_count:
            raise ValueError("language counts exceed selectable_file_count")
        return self


class ContextStatistics(BaseModel):
    """Deterministic aggregate statistics for one complete package."""

    tree_file_count: NonNegativeInt
    tree_directory_count: NonNegativeInt
    selected_file_count: NonNegativeInt
    ranged_file_count: NonNegativeInt
    selected_source_bytes: NonNegativeInt
    included_content_bytes: NonNegativeInt
    included_character_count: NonNegativeInt
    included_line_count: NonNegativeInt
    languages: dict[str, NonNegativeInt] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("languages")
    @classmethod
    def validate_languages(
        cls, languages: dict[str, NonNegativeInt]
    ) -> dict[str, NonNegativeInt]:
        """Require canonical language-key order."""

        _validate_language_counts(languages)
        return languages

    @property
    def item_count(self) -> int:
        """Return the selected item/file count."""

        return self.selected_file_count

    @property
    def total_source_bytes(self) -> int:
        """Return total raw source bytes represented by selected items."""

        return self.selected_source_bytes

    @property
    def total_content_bytes(self) -> int:
        """Return total canonical included UTF-8 bytes."""

        return self.included_content_bytes

    @property
    def character_count(self) -> int:
        """Return the number of Unicode code points in included content."""

        return self.included_character_count

    @property
    def line_count(self) -> int:
        """Return included logical source lines."""

        return self.included_line_count


class ContextPackage(BaseModel):
    """The sole canonical successful result of context construction."""

    schema_version: Literal[1] = CONTEXT_PACKAGE_SCHEMA_VERSION
    title: str = Field(
        default=DEFAULT_CONTEXT_TASK,
        validation_alias=AliasChoices("title", "task_description"),
    )
    project: ContextProject
    tree: ProjectTree | None = None
    files: tuple[ContextFile, ...] = Field(
        validation_alias=AliasChoices("files", "items")
    )
    statistics: ContextStatistics

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("title")
    @classmethod
    def validate_title(cls, title: str) -> str:
        """Canonicalize and validate the explicit task description."""

        canonical = title.strip()
        if not canonical:
            raise ValueError("task description must not be empty")
        if any(ord(character) < 32 and character != "\t" for character in canonical):
            raise ValueError("task description contains an ASCII control character")
        if "\x7f" in canonical:
            raise ValueError("task description contains an ASCII control character")
        return canonical

    @model_validator(mode="after")
    def validate_package(self) -> ContextPackage:
        """Recompute package ordering, membership, and every statistic."""

        paths = tuple(file.path for file in self.files)
        if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
            raise ValueError("files must have unique paths in canonical order")

        selected_file_count = len(self.files)
        if selected_file_count > self.project.selectable_file_count:
            raise ValueError("selected files exceed selectable_file_count")

        selected_source_bytes = sum(file.source_size_bytes for file in self.files)
        if selected_source_bytes > self.project.selectable_source_bytes:
            raise ValueError("selected source bytes exceed selectable_source_bytes")

        selected_directories: set[str] = set()
        for path in paths:
            parts = path.split("/")
            selected_directories.update(
                "/".join(parts[:index]) for index in range(1, len(parts))
            )
        selected_directory_count = len(selected_directories)
        if selected_directory_count > self.project.selectable_directory_count:
            raise ValueError(
                "selected path directories exceed selectable_directory_count"
            )

        selected_languages = Counter(
            file.language for file in self.files if file.language is not None
        )
        if any(
            count > self.project.languages.get(language, 0)
            for language, count in selected_languages.items()
        ):
            raise ValueError("selected language counts exceed project languages")

        if self.tree is not None:
            tree_paths = {
                entry.path for entry in self.tree.entries if entry.kind == "file"
            }
            missing_paths = tuple(path for path in paths if path not in tree_paths)
            if missing_paths:
                raise ValueError("selected file path is absent from the project tree")
            if self.tree.file_count != self.project.selectable_file_count:
                raise ValueError("tree file count does not match project metadata")
            if self.tree.directory_count != self.project.selectable_directory_count:
                raise ValueError("tree directory count does not match project metadata")

        expected_statistics = calculate_context_statistics(self.project, self.files)
        if self.statistics != expected_statistics:
            raise ValueError("statistics do not match package content")
        return self

    @property
    def task_description(self) -> str:
        """Return the package task using the request-oriented terminology."""

        return self.title

    @property
    def items(self) -> tuple[ContextItem, ...]:
        """Return selected files as context items."""

        return self.files


def canonical_line_count(text: str) -> int:
    """Count logical lines without treating a final LF as an extra empty line.

    Empty text has zero lines. Otherwise the count is the number of LF
    separators plus one when the final logical line does not end in LF. Thus
    ``"a\n"`` has one line, ``"a\nb"`` has two, and ``"\n"`` has one.
    """

    if not text:
        return 0
    return text.count("\n") + (not text.endswith("\n"))


def calculate_context_statistics(
    project: ContextProject, files: tuple[ContextFile, ...]
) -> ContextStatistics:
    """Derive every stored package statistic from canonical domain values."""

    languages = Counter(file.language for file in files if file.language is not None)
    return ContextStatistics(
        tree_file_count=project.selectable_file_count,
        tree_directory_count=project.selectable_directory_count,
        selected_file_count=len(files),
        ranged_file_count=sum(file.selection == "ranges" for file in files),
        selected_source_bytes=sum(file.source_size_bytes for file in files),
        included_content_bytes=sum(file.included_content_bytes for file in files),
        included_character_count=sum(
            len(block.text) for file in files for block in file.blocks
        ),
        included_line_count=sum(file.included_line_count for file in files),
        languages=dict(sorted(languages.items())),
    )


def validate_portable_package_path(path: str) -> str:
    """Validate an already-canonical portable repository-relative path."""

    if (
        not isinstance(path, str)
        or "\\" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise ValueError("package path must be a portable relative path")
    if not path or path.startswith("/") or _WINDOWS_DRIVE.match(path):
        raise ValueError("package path must be a portable relative path")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("package path must be a portable relative path")
    return path


def _validate_language_counts(languages: dict[str, int]) -> None:
    if tuple(languages) != tuple(sorted(languages)):
        raise ValueError("language counts must use canonical key order")
    for language in languages:
        _validate_language_label(language)


def _validate_language_label(language: str) -> None:
    if not language:
        raise ValueError("language labels must not be empty")
    if any(ord(character) < 32 or ord(character) == 127 for character in language):
        raise ValueError("language labels must not contain ASCII control characters")
