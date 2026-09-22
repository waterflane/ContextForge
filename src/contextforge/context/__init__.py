# ruff: noqa: F401
# mypy: implicit_reexport = True
"""Lazy public boundary for context selection, packages, and capsules."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from contextforge.context.builder import (
        ContextBuilder,
        ContextBuildError,
        ContextBuildLimitError,
        ContextBuildOptions,
        ContextContentByteLimitError,
        ContextFileCountLimitError,
        ContextSourceByteLimitError,
        build_context_package,
    )
    from contextforge.context.capsule import (
        CapsuleMaterial,
        CapsuleRange,
        CapsuleSnapshot,
        CompilationSufficiency,
        CompiledContextCapsule,
        ConservativeTokenEstimator,
        ContextBudget,
        ContextBudgetError,
        ContextCapsule,
        ContextCompilerError,
        ContextFreshnessError,
        RepresentationMode,
        TokenEstimator,
        compile_context_capsule,
    )
    from contextforge.context.inspection import (
        ContextInspection,
        ContextInspectionError,
        ContextInspectionItem,
        PackageReadError,
        PackageValidationError,
        UnsupportedSchemaVersionError,
        inspect_context_package,
        inspect_context_package_json,
        load_context_package_json,
        render_context_inspection,
        validate_context_package,
    )
    from contextforge.context.package import (
        ContextBlock,
        ContextFile,
        ContextItem,
        ContextPackage,
        ContextProject,
        ContextStatistics,
        calculate_context_statistics,
        canonical_line_count,
    )
    from contextforge.context.reader import (
        ContextLimitError,
        ContextReaderError,
        FileChangedError,
        LineRangeBoundsError,
        ReaderLimits,
        SelectedFileChangedError,
        SelectedFileDecodeError,
        SelectedFileMissingError,
        SelectedFileNotInSnapshotError,
        SelectedFileNotRegularError,
        SelectedFileOutsideRootError,
        SelectedFileReadError,
        SelectedFileTooLargeError,
        SelectedTextBlock,
        SelectedTextFile,
        TextDecodingError,
        read_selected_text_file,
        read_selected_text_files,
    )
    from contextforge.context.renderers import (
        ContextRenderError,
        render_context_package_json,
        render_context_package_markdown,
    )
    from contextforge.context.selection import (
        ContextSelection,
        DuplicateSnapshotPathError,
        InvalidLineRangeError,
        InvalidSelectorError,
        LineRange,
        LineRangeRequest,
        LineRangeTargetError,
        NoFilesSelectedError,
        SelectionError,
        SelectionResult,
        SelectionSelector,
        SelectorMatch,
        SelectorNoMatchError,
        canonicalize_line_ranges,
        parse_line_range_request,
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

_MODULE_EXPORTS = {
    "builder": (
        "ContextBuilder",
        "ContextBuildError",
        "ContextBuildLimitError",
        "ContextBuildOptions",
        "ContextContentByteLimitError",
        "ContextFileCountLimitError",
        "ContextSourceByteLimitError",
        "build_context_package",
    ),
    "capsule": (
        "AUTOMATIC_FULL_FILE_MAX_LINES",
        "AUTOMATIC_CONTEXT_SOFT_RATIO",
        "CONTEXT_CAPSULE_SCHEMA_VERSION",
        "SLICE_CONTEXT_LINES",
        "SLICE_MERGE_GAP",
        "CapsuleMaterial",
        "CapsuleRange",
        "CapsuleSnapshot",
        "CompilationSufficiency",
        "CompiledContextCapsule",
        "ConservativeTokenEstimator",
        "ContextBudget",
        "ContextBudgetError",
        "ContextCapsule",
        "ContextCompilerError",
        "ContextFreshnessError",
        "RepresentationMode",
        "TokenEstimator",
        "compile_context_capsule",
    ),
    "inspection": (
        "ContextInspection",
        "ContextInspectionError",
        "ContextInspectionItem",
        "PackageReadError",
        "PackageValidationError",
        "UnsupportedSchemaVersionError",
        "inspect_context_package",
        "inspect_context_package_json",
        "load_context_package_json",
        "render_context_inspection",
        "validate_context_package",
    ),
    "package": (
        "CONTEXT_PACKAGE_SCHEMA_VERSION",
        "DEFAULT_CONTEXT_TASK",
        "ContextBlock",
        "ContextFile",
        "ContextItem",
        "ContextPackage",
        "ContextProject",
        "ContextStatistics",
        "calculate_context_statistics",
        "canonical_line_count",
    ),
    "reader": (
        "ContextLimitError",
        "ContextReaderError",
        "FileChangedError",
        "LineRangeBoundsError",
        "ReaderLimits",
        "SelectedFileChangedError",
        "SelectedFileDecodeError",
        "SelectedFileMissingError",
        "SelectedFileNotInSnapshotError",
        "SelectedFileNotRegularError",
        "SelectedFileOutsideRootError",
        "SelectedFileReadError",
        "SelectedFileTooLargeError",
        "SelectedTextBlock",
        "SelectedTextFile",
        "TextDecodingError",
        "read_selected_text_file",
        "read_selected_text_files",
    ),
    "renderers": (
        "MAX_JSON_PACKAGE_BYTES",
        "ContextRenderError",
        "render_context_package_json",
        "render_context_package_markdown",
    ),
    "selection": (
        "MAX_LINE_NUMBER",
        "ContextSelection",
        "DuplicateSnapshotPathError",
        "InvalidLineRangeError",
        "InvalidSelectorError",
        "LineRange",
        "LineRangeRequest",
        "LineRangeTargetError",
        "NoFilesSelectedError",
        "SelectionError",
        "SelectionResult",
        "SelectionSelector",
        "SelectorMatch",
        "SelectorNoMatchError",
        "canonicalize_line_ranges",
        "parse_line_range_request",
        "resolve_selection",
        "select_files",
    ),
    "tree": (
        "ProjectTree",
        "ProjectTreeEntry",
        "ProjectTreeError",
        "build_project_tree",
        "render_project_tree",
        "render_project_tree_json",
        "render_project_tree_markdown",
    ),
}

_LAZY_EXPORTS = {
    name: f"contextforge.context.{module}"
    for module, names in _MODULE_EXPORTS.items()
    for name in names
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = sorted(_LAZY_EXPORTS)
