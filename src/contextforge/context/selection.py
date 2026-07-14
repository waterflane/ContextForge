"""Deterministic file selection over a repository snapshot.

Selection is purely an inventory operation: this module never joins selectors to
the snapshot root and never consults the filesystem. Exact paths and directories
are explicit selector kinds. Globs use PathSpec's case-sensitive GitWildMatch
syntax against portable snapshot paths; slashless patterns match at any depth,
while patterns containing a slash are rooted at the snapshot root.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Literal

from pathspec.patterns.gitwildmatch import (
    GitWildMatchPattern,
    GitWildMatchPatternError,
)
from pydantic import BaseModel, ConfigDict

from contextforge.repositories import ProjectFile, ProjectSnapshot
from contextforge.repositories.files import normalize_relative_path

IncludeSelectorKind = Literal["exact_path", "directory", "glob"]
SelectorMatchKind = Literal["exact_path", "directory", "glob", "exclusion"]

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class SelectionError(ValueError):
    """Base class for deterministic selection failures."""


class InvalidSelectorError(SelectionError):
    """Raised when a selector is unsafe or malformed."""

    def __init__(self, selector_type: SelectorMatchKind, selector: object) -> None:
        self.selector_type = selector_type
        self.selector = selector
        super().__init__(f"invalid {selector_type} selector: {selector!r}")


class SelectorNoMatchError(SelectionError):
    """Raised when a required include selector matches no snapshot file."""

    def __init__(self, selector: SelectionSelector) -> None:
        self.selector = selector
        super().__init__(
            f"{selector.kind} selector matched no snapshot file: {selector.value!r}"
        )


class NoFilesSelectedError(SelectionError):
    """Raised when the final include-minus-exclude set is empty."""

    def __init__(self, excluded_files: tuple[ProjectFile, ...] = ()) -> None:
        self.excluded_files = excluded_files
        super().__init__("selection contains no files after exclusions")


class DuplicateSnapshotPathError(SelectionError):
    """Raised when snapshot entries do not have unique portable paths."""

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__(f"duplicate snapshot path: {path}")


class SelectionSelector(BaseModel):
    """One explicitly typed include selector."""

    kind: IncludeSelectorKind
    value: str

    model_config = ConfigDict(frozen=True, extra="forbid")


class ContextSelection(BaseModel):
    """Manual selection request using the categories from the context plan."""

    exact_paths: tuple[str, ...] = ()
    directories: tuple[str, ...] = ()
    globs: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True, extra="forbid")


class SelectorMatch(BaseModel):
    """Files matched by one selector before include/exclude precedence."""

    kind: SelectorMatchKind
    selector: str
    normalized_selector: str
    files: tuple[ProjectFile, ...] = ()

    model_config = ConfigDict(frozen=True, extra="forbid")


class SelectionResult(BaseModel):
    """Canonical selection plus enough detail to explain how it was produced."""

    files: tuple[ProjectFile, ...]
    include_matches: tuple[SelectorMatch, ...] = ()
    exclusion_matches: tuple[SelectorMatch, ...] = ()
    excluded_files: tuple[ProjectFile, ...] = ()

    model_config = ConfigDict(frozen=True, extra="forbid")

    @property
    def unmatched_exclusions(self) -> tuple[str, ...]:
        """Return accepted exclusions that matched no snapshot file."""

        return tuple(
            match.selector for match in self.exclusion_matches if not match.files
        )


def resolve_selection(
    snapshot: ProjectSnapshot, selection: ContextSelection
) -> SelectionResult:
    """Resolve an explicit context selection solely against ``snapshot.files``."""

    includes = (
        *(
            SelectionSelector(kind="exact_path", value=value)
            for value in selection.exact_paths
        ),
        *(
            SelectionSelector(kind="directory", value=value)
            for value in selection.directories
        ),
        *(SelectionSelector(kind="glob", value=value) for value in selection.globs),
    )
    return select_files(snapshot, includes, selection.exclusions)


def select_files(
    snapshot: ProjectSnapshot,
    include_selectors: Iterable[SelectionSelector] = (),
    exclude_selectors: Iterable[str] = (),
) -> SelectionResult:
    """Select snapshot files using typed includes followed by glob exclusions.

    Include selectors are unioned and deduplicated. With no includes, all snapshot
    files form the initial set. Every include must independently match before any
    exclusion is applied; unmatched exclusions are intentionally accepted. The
    returned file tuples are sorted by case-sensitive portable relative path.
    """

    files_by_path = _snapshot_index(snapshot)
    ordered_paths = tuple(sorted(files_by_path))
    includes = tuple(include_selectors)

    include_matches: list[SelectorMatch] = []
    included_paths: set[str] = set(ordered_paths) if not includes else set()
    for selector in includes:
        if not isinstance(selector, SelectionSelector):
            raise InvalidSelectorError("exact_path", selector)
        match = _match_include(selector, ordered_paths, files_by_path)
        if not match.files:
            raise SelectorNoMatchError(selector)
        include_matches.append(match)
        included_paths.update(file.path for file in match.files)

    exclusion_matches: list[SelectorMatch] = []
    excluded_paths: set[str] = set()
    for raw_selector in exclude_selectors:
        normalized, pattern = _compile_glob("exclusion", raw_selector)
        matched_paths = tuple(
            path for path in ordered_paths if pattern.match_file(path) is not None
        )
        matched_files = tuple(files_by_path[path] for path in matched_paths)
        exclusion_matches.append(
            SelectorMatch(
                kind="exclusion",
                selector=raw_selector,
                normalized_selector=normalized,
                files=matched_files,
            )
        )
        excluded_paths.update(included_paths.intersection(matched_paths))

    final_paths = tuple(sorted(included_paths - excluded_paths))
    excluded_files = tuple(files_by_path[path] for path in sorted(excluded_paths))
    if not final_paths:
        raise NoFilesSelectedError(excluded_files)

    return SelectionResult(
        files=tuple(files_by_path[path] for path in final_paths),
        include_matches=tuple(include_matches),
        exclusion_matches=tuple(exclusion_matches),
        excluded_files=excluded_files,
    )


def _snapshot_index(snapshot: ProjectSnapshot) -> dict[str, ProjectFile]:
    files_by_path: dict[str, ProjectFile] = {}
    for file in snapshot.files:
        if not isinstance(file, ProjectFile) or not isinstance(file.path, str):
            raise SelectionError("snapshot entries must be ProjectFile instances")
        path = _validate_snapshot_path(file.path)
        if path in files_by_path:
            raise DuplicateSnapshotPathError(path)
        files_by_path[path] = file
    return files_by_path


def _validate_snapshot_path(path: str) -> str:
    portable = path.replace("\\", "/")
    if (
        not portable
        or portable != path
        or "\x00" in portable
        or portable.startswith("/")
        or _WINDOWS_DRIVE.match(portable)
    ):
        raise SelectionError(f"snapshot path is not portable: {path!r}")
    if any(part in {"", ".", ".."} for part in portable.split("/")):
        raise SelectionError(f"snapshot path is not portable: {path!r}")
    return portable


def _match_include(
    selector: SelectionSelector,
    ordered_paths: tuple[str, ...],
    files_by_path: dict[str, ProjectFile],
) -> SelectorMatch:
    matched_paths: tuple[str, ...]
    if selector.kind == "exact_path":
        normalized = _normalize_path_selector(
            selector.kind, selector.value, allow_root=False
        )
        matched_paths = (normalized,) if normalized in files_by_path else ()
    elif selector.kind == "directory":
        normalized = _normalize_path_selector(
            selector.kind, selector.value, allow_root=True
        )
        matched_paths = (
            ordered_paths
            if normalized == "."
            else tuple(
                path for path in ordered_paths if path.startswith(f"{normalized}/")
            )
        )
    else:
        normalized, pattern = _compile_glob(selector.kind, selector.value)
        matched_paths = tuple(
            path for path in ordered_paths if pattern.match_file(path) is not None
        )

    return SelectorMatch(
        kind=selector.kind,
        selector=selector.value,
        normalized_selector=normalized,
        files=tuple(files_by_path[path] for path in matched_paths),
    )


def _normalize_path_selector(
    selector_type: Literal["exact_path", "directory"],
    selector: object,
    *,
    allow_root: bool,
) -> str:
    if not isinstance(selector, str) or "\x00" in selector:
        raise InvalidSelectorError(selector_type, selector)

    portable = selector.replace("\\", "/")
    if not portable or portable.startswith("/") or _WINDOWS_DRIVE.match(portable):
        raise InvalidSelectorError(selector_type, selector)

    parts = portable.split("/")
    if ".." in parts:
        raise InvalidSelectorError(selector_type, selector)
    normalized_parts = [part for part in parts if part not in {"", "."}]
    if not normalized_parts:
        if allow_root:
            return "."
        raise InvalidSelectorError(selector_type, selector)

    try:
        return normalize_relative_path("/".join(normalized_parts))
    except ValueError as exc:  # pragma: no cover - guarded above, kept fail-closed
        raise InvalidSelectorError(selector_type, selector) from exc


def _compile_glob(
    selector_type: Literal["glob", "exclusion"], selector: object
) -> tuple[str, GitWildMatchPattern]:
    if not isinstance(selector, str) or "\x00" in selector:
        raise InvalidSelectorError(selector_type, selector)

    normalized = selector.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith(("/", "!"))
        or _WINDOWS_DRIVE.match(normalized)
    ):
        raise InvalidSelectorError(selector_type, selector)
    if any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise InvalidSelectorError(selector_type, selector)

    try:
        pattern = GitWildMatchPattern(normalized)
    except (GitWildMatchPatternError, re.error) as exc:
        raise InvalidSelectorError(selector_type, selector) from exc
    if pattern.include is not True or pattern.regex is None:
        raise InvalidSelectorError(selector_type, selector)
    return normalized, pattern
