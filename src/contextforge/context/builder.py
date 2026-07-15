"""Deterministic orchestration for canonical context packages."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from contextforge.context.package import (
    DEFAULT_CONTEXT_TASK,
    ContextBlock,
    ContextFile,
    ContextPackage,
    ContextProject,
    calculate_context_statistics,
)
from contextforge.context.reader import (
    ContextLimitError,
    ReaderLimits,
    SelectedTextFile,
    read_selected_text_file,
)
from contextforge.context.selection import ContextSelection, resolve_selection
from contextforge.context.tree import build_project_tree
from contextforge.repositories import ProjectSnapshot, scan_repository

PositiveInt = Annotated[int, Field(gt=0, strict=True)]
ContextSource = str | Path | ProjectSnapshot


class ContextBuildError(ValueError):
    """Base class for deterministic context-build failures."""


class ContextBuildLimitError(ContextBuildError, ContextLimitError):
    """Base class for a configured builder safeguard being exceeded."""

    def __init__(
        self,
        limit_name: str,
        configured_limit: int,
        observed_value: int,
        *,
        path: str | None = None,
    ) -> None:
        self.limit_name = limit_name
        self.configured_limit = configured_limit
        self.observed_value = observed_value
        self.path = path
        location = f" for {path!r}" if path is not None else ""
        super().__init__(
            f"{limit_name} exceeded{location}: configured limit "
            f"{configured_limit}, observed {observed_value}"
        )


class ContextFileCountLimitError(ContextBuildLimitError):
    """Raised before reads when too many deduplicated files are selected."""


class ContextSourceByteLimitError(ContextBuildLimitError):
    """Raised before reads when a selected raw source file is too large."""


class ContextContentByteLimitError(ContextBuildLimitError):
    """Raised while canonical content is accumulated in file order."""


class ContextBuildOptions(BaseModel):
    """Explicit deterministic inputs and safeguards for package construction."""

    title: str = Field(
        default=DEFAULT_CONTEXT_TASK,
        validation_alias=AliasChoices("title", "task_description"),
    )
    selection: ContextSelection = Field(default_factory=ContextSelection)
    include_tree: bool = True
    max_files: PositiveInt = 100
    max_source_bytes_per_file: PositiveInt = 1_000_000
    max_total_content_bytes: PositiveInt = 1_000_000

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("title")
    @classmethod
    def validate_title(cls, title: str) -> str:
        """Apply the package task policy before any repository work."""

        canonical = title.strip()
        if not canonical:
            raise ValueError("task description must not be empty")
        if any(ord(character) < 32 and character != "\t" for character in canonical):
            raise ValueError("task description contains an ASCII control character")
        if "\x7f" in canonical:
            raise ValueError("task description contains an ASCII control character")
        return canonical

    @property
    def task_description(self) -> str:
        """Return the task using request-oriented terminology."""

        return self.title


class ContextBuilder:
    """Build one complete package from a root or an approved snapshot."""

    def __init__(
        self,
        source: ContextSource | None = None,
        options: ContextBuildOptions | None = None,
    ) -> None:
        self._source = source
        self._options = options if options is not None else ContextBuildOptions()

    def build(self, source: ContextSource | None = None) -> ContextPackage:
        """Return a complete package or raise without returning partial state."""

        active_source = source if source is not None else self._source
        if active_source is None:
            raise ContextBuildError("a repository root or ProjectSnapshot is required")
        snapshot = (
            active_source
            if isinstance(active_source, ProjectSnapshot)
            else scan_repository(active_source)
        )
        return self._build_snapshot(snapshot)

    def _build_snapshot(self, snapshot: ProjectSnapshot) -> ContextPackage:
        options = self._options
        selection = resolve_selection(snapshot, options.selection)
        selected_count = len(selection.files)
        if selected_count > options.max_files:
            raise ContextFileCountLimitError(
                "maximum selected files",
                options.max_files,
                selected_count,
            )

        for project_file in selection.files:
            if project_file.size_bytes > options.max_source_bytes_per_file:
                raise ContextSourceByteLimitError(
                    "maximum source bytes per file",
                    options.max_source_bytes_per_file,
                    project_file.size_bytes,
                    path=project_file.path,
                )

        tree = build_project_tree(snapshot)
        project = _build_project_metadata(snapshot, tree.directory_count)
        ranges_by_path = {
            path: tuple(
                request.range
                for request in selection.line_ranges
                if request.path == path
            )
            for path in {request.path for request in selection.line_ranges}
        }

        files: list[ContextFile] = []
        total_content_bytes = 0
        for project_file in selection.files:
            selected = read_selected_text_file(
                snapshot,
                project_file,
                line_ranges=ranges_by_path.get(project_file.path, ()),
                limits=ReaderLimits(
                    max_files=options.max_files,
                    max_source_bytes=options.max_source_bytes_per_file,
                    # Canonical UTF-8 content never exceeds verified raw bytes.
                    max_content_bytes=max(project_file.size_bytes, 1),
                ),
            )
            observed_total = total_content_bytes + selected.included_content_bytes
            if observed_total > options.max_total_content_bytes:
                raise ContextContentByteLimitError(
                    "maximum total content bytes",
                    options.max_total_content_bytes,
                    observed_total,
                    path=project_file.path,
                )
            total_content_bytes = observed_total
            files.append(_build_context_file(selected))

        canonical_files = tuple(files)
        statistics = calculate_context_statistics(project, canonical_files)
        return ContextPackage(
            title=options.title,
            project=project,
            tree=tree if options.include_tree else None,
            files=canonical_files,
            statistics=statistics,
        )


def build_context_package(
    source: ContextSource, options: ContextBuildOptions | None = None
) -> ContextPackage:
    """Convenience function for one deterministic context-package build."""

    return ContextBuilder(source, options).build()


def _build_project_metadata(
    snapshot: ProjectSnapshot, directory_count: int
) -> ContextProject:
    languages = Counter(
        file.language for file in snapshot.files if file.language is not None
    )
    return ContextProject(
        selectable_file_count=len(snapshot.files),
        selectable_directory_count=directory_count,
        selectable_source_bytes=sum(file.size_bytes for file in snapshot.files),
        languages=dict(sorted(languages.items())),
    )


def _build_context_file(selected: SelectedTextFile) -> ContextFile:
    blocks = tuple(
        ContextBlock(
            start_line=(
                block.line_range.start if block.line_range is not None else None
            ),
            end_line=(block.line_range.end if block.line_range is not None else None),
            text=block.text,
            line_count=block.line_count,
            size_bytes=block.size_bytes,
            sha256=block.sha256,
        )
        for block in selected.blocks
    )
    return ContextFile(
        path=selected.project_file.path,
        language=selected.project_file.language,
        source_size_bytes=selected.project_file.size_bytes,
        source_sha256=selected.project_file.sha256,
        source_line_count=selected.source_line_count,
        selection="ranges" if blocks[0].start_line is not None else "full",
        blocks=blocks,
        included_line_count=selected.included_line_count,
        included_content_bytes=selected.included_content_bytes,
    )
