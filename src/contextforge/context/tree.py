"""Deterministic project trees derived from repository snapshots."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contextforge.repositories import ProjectFile, ProjectSnapshot

NonNegativeInt = Annotated[int, Field(ge=0)]
TreeEntryKind = Literal["directory", "file"]

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class ProjectTreeError(ValueError):
    """Raised when an inventory cannot form a portable project tree."""


class ProjectTreeEntry(BaseModel):
    """One portable directory or file entry below the implicit root."""

    path: str
    kind: TreeEntryKind

    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("path")
    @classmethod
    def validate_path(cls, path: str) -> str:
        """Store a strictly portable relative path."""

        return _portable_inventory_path(path)


class ProjectTree(BaseModel):
    """A canonical, immutable pre-order project tree."""

    entries: tuple[ProjectTreeEntry, ...] = ()
    file_count: NonNegativeInt = 0
    directory_count: NonNegativeInt = 0

    model_config = ConfigDict(frozen=True, extra="forbid")

    @model_validator(mode="after")
    def validate_canonical_tree(self) -> ProjectTree:
        """Reject inconsistent counts, paths, and entry ordering."""

        actual = tuple((entry.path, entry.kind) for entry in self.entries)
        file_paths = tuple(entry.path for entry in self.entries if entry.kind == "file")
        expected = _canonical_entry_specs(file_paths)
        if actual != expected:
            raise ValueError("tree entries must be unique and in canonical pre-order")
        if self.file_count != len(file_paths):
            raise ValueError("file_count does not match the tree entries")
        expected_directory_count = len(expected) - len(file_paths)
        if self.directory_count != expected_directory_count:
            raise ValueError("directory_count does not match the tree entries")
        return self


@dataclass(slots=True)
class _Directory:
    directories: dict[str, _Directory] = field(default_factory=dict)
    files: set[str] = field(default_factory=set)


def build_project_tree(
    source: ProjectSnapshot | Iterable[ProjectFile],
) -> ProjectTree:
    """Build a project tree solely from a snapshot's included-file inventory.

    No filesystem operation is performed. Directory entries are synthesized
    only from the portable paths already present in the supplied inventory.
    """

    inventory = source.files if isinstance(source, ProjectSnapshot) else tuple(source)
    paths: list[str] = []
    for item in inventory:
        if not isinstance(item, ProjectFile):
            raise ProjectTreeError("inventory entries must be ProjectFile instances")
        paths.append(_portable_inventory_path(item.path))

    specs = _canonical_entry_specs(paths)
    entries = tuple(ProjectTreeEntry(path=path, kind=kind) for path, kind in specs)
    return ProjectTree(
        entries=entries,
        file_count=len(paths),
        directory_count=len(entries) - len(paths),
    )


def render_project_tree(tree: ProjectTree, *, max_depth: int | None = None) -> str:
    """Render the normative ASCII representation with one final newline.

    Depth is the number of path segments (edges below the implicit ``.`` root).
    ``None`` is unlimited, zero shows only the root, and entries deeper than a
    non-negative limit are omitted without an ellipsis.
    """

    _validate_max_depth(max_depth)
    children = _children_by_parent(tree)
    lines = ["."]

    def add_children(parent: str, prefix: str, parent_depth: int) -> None:
        if max_depth is not None and parent_depth >= max_depth:
            return
        child_entries = children.get(parent, ())
        for index, entry in enumerate(child_entries):
            is_last = index == len(child_entries) - 1
            connector = "`-- " if is_last else "|-- "
            suffix = "/" if entry.kind == "directory" else ""
            lines.append(f"{prefix}{connector}{_entry_name(entry.path)}{suffix}")
            if entry.kind == "directory":
                extension = "    " if is_last else "|   "
                add_children(entry.path, prefix + extension, parent_depth + 1)

    add_children("", "", 0)
    return "\n".join(lines) + "\n"


def render_project_tree_markdown(
    tree: ProjectTree, *, max_depth: int | None = None
) -> str:
    """Render a project tree for Markdown using the normative visual tree."""

    return render_project_tree(tree, max_depth=max_depth)


def render_project_tree_json(tree: ProjectTree, *, max_depth: int | None = None) -> str:
    """Render the stable schema-versioned JSON project-tree representation."""

    _validate_max_depth(max_depth)
    visible_entries = tuple(
        entry
        for entry in tree.entries
        if max_depth is None or _entry_depth(entry.path) <= max_depth
    )
    payload = {
        "schema_version": 1,
        "root": ".",
        "max_depth": max_depth,
        "file_count": sum(entry.kind == "file" for entry in visible_entries),
        "directory_count": sum(entry.kind == "directory" for entry in visible_entries),
        "entries": [entry.model_dump(mode="json") for entry in visible_entries],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _canonical_entry_specs(
    file_paths: Iterable[str],
) -> tuple[tuple[str, TreeEntryKind], ...]:
    root = _Directory()
    seen_files: set[str] = set()
    for raw_path in file_paths:
        path = _portable_inventory_path(raw_path)
        if path in seen_files:
            raise ProjectTreeError(f"duplicate inventory path: {path}")
        seen_files.add(path)

        parts = path.split("/")
        directory = root
        for index, part in enumerate(parts[:-1]):
            if part in directory.files:
                collision = "/".join(parts[: index + 1])
                raise ProjectTreeError(
                    f"inventory path is both a file and directory: {collision}"
                )
            directory = directory.directories.setdefault(part, _Directory())

        filename = parts[-1]
        if filename in directory.directories:
            raise ProjectTreeError(
                f"inventory path is both a file and directory: {path}"
            )
        directory.files.add(filename)

    entries: list[tuple[str, TreeEntryKind]] = []

    def visit(directory: _Directory, parent: str) -> None:
        for name in sorted(directory.directories):
            path = f"{parent}/{name}" if parent else name
            entries.append((path, "directory"))
            visit(directory.directories[name], path)
        for name in sorted(directory.files):
            path = f"{parent}/{name}" if parent else name
            entries.append((path, "file"))

    visit(root, "")
    return tuple(entries)


def _portable_inventory_path(path: str) -> str:
    if not isinstance(path, str):
        raise ProjectTreeError("inventory path must be a string")
    if "\x00" in path:
        raise ProjectTreeError("inventory path must not contain NUL")

    portable = path.replace("\\", "/")
    if not portable or portable.startswith("/") or _WINDOWS_DRIVE.match(portable):
        raise ProjectTreeError(f"inventory path must be relative: {path!r}")

    parts = portable.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ProjectTreeError(f"inventory path is malformed: {path!r}")
    return "/".join(parts)


def _children_by_parent(
    tree: ProjectTree,
) -> dict[str, tuple[ProjectTreeEntry, ...]]:
    children: defaultdict[str, list[ProjectTreeEntry]] = defaultdict(list)
    for entry in tree.entries:
        parent, _, _ = entry.path.rpartition("/")
        children[parent].append(entry)
    return {parent: tuple(entries) for parent, entries in children.items()}


def _entry_name(path: str) -> str:
    return path.rsplit("/", maxsplit=1)[-1]


def _entry_depth(path: str) -> int:
    return path.count("/") + 1


def _validate_max_depth(max_depth: int | None) -> None:
    if max_depth is not None and (
        isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0
    ):
        raise ProjectTreeError("maximum depth must be a non-negative integer or None")
