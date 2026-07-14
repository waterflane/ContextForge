"""Context package and deterministic project-tree domain boundary."""

from contextforge.context.package import ContextPackage
from contextforge.context.selection import (
    ContextSelection,
    DuplicateSnapshotPathError,
    InvalidSelectorError,
    NoFilesSelectedError,
    SelectionError,
    SelectionResult,
    SelectionSelector,
    SelectorMatch,
    SelectorNoMatchError,
    resolve_selection,
    select_files,
)
from contextforge.context.tree import (
    ProjectTree,
    ProjectTreeEntry,
    ProjectTreeError,
    build_project_tree,
    render_project_tree,
    render_project_tree_json,
    render_project_tree_markdown,
)

__all__ = [
    "ContextPackage",
    "ContextSelection",
    "DuplicateSnapshotPathError",
    "InvalidSelectorError",
    "NoFilesSelectedError",
    "ProjectTree",
    "ProjectTreeEntry",
    "ProjectTreeError",
    "SelectionError",
    "SelectionResult",
    "SelectionSelector",
    "SelectorMatch",
    "SelectorNoMatchError",
    "build_project_tree",
    "render_project_tree",
    "render_project_tree_json",
    "render_project_tree_markdown",
    "resolve_selection",
    "select_files",
]
