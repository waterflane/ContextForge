"""Validated read-only discovery tools over one pinned repository snapshot."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contextforge.context import (
    LineRange,
    ReaderLimits,
    SelectedTextFile,
    read_selected_text_file,
)
from contextforge.core.validation import validate_portable_relative_path
from contextforge.intelligence import (
    ArchitectureMap,
    FeatureMap,
    FileCodeMap,
    FileSemanticAnalysis,
    IndexManifest,
    RepositoryOverview,
    calculate_source_snapshot_digest,
)
from contextforge.intelligence.codemap import SymbolRecord
from contextforge.repositories import ProjectFile, ProjectSnapshot

from .models import (
    DiscoveryBudget,
    DiscoveryBudgetUsage,
    DiscoveryCandidate,
    DiscoveryLineRange,
    DiscoveryMode,
    DiscoveryObservation,
    SelectionReason,
)

MAX_LIST_RESULT_BYTES = 64 * 1024
MAX_SEARCH_RESULT_BYTES = 128 * 1024
MAX_SUMMARY_RESULT_BYTES = 64 * 1024
MAX_READ_FILE_BYTES = 256 * 1024
MAX_READ_LINES_BYTES = 128 * 1024
MAX_READ_LINES = 500
MAX_GIT_DIFF_BYTES = 256 * 1024
MAX_RESULT_LIMIT = 100

_SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+~-]{0,199}$")


class ToolInput(BaseModel):
    # JSON arrays are normalized to immutable tuples; scalar limits remain strict.
    model_config = ConfigDict(extra="forbid")


class EmptyInput(ToolInput):
    pass


class PageInput(ToolInput):
    cursor: str | None = None
    limit: int = Field(default=20, ge=1, le=MAX_RESULT_LIMIT, strict=True)


class ListTreeInput(PageInput):
    path: str | None = None
    depth: int = Field(default=3, ge=0, le=8, strict=True)


class SearchIndexInput(PageInput):
    query: str = Field(min_length=1, max_length=500)
    kinds: tuple[str, ...] = ()


class SearchSymbolsInput(PageInput):
    query: str = Field(min_length=1, max_length=500)
    kinds: tuple[str, ...] = ()
    path_prefix: str | None = None


class SearchTextInput(PageInput):
    query: str = Field(min_length=1, max_length=500)
    path_glob: str | None = None
    case_sensitive: bool = False


class PathInput(ToolInput):
    path: str


class PagedPathInput(PageInput):
    path: str


class SymbolInput(ToolInput):
    symbol_id: str = Field(min_length=1, max_length=500)


class PagedSymbolInput(PageInput):
    symbol_id: str = Field(min_length=1, max_length=500)


class RelatedTestsInput(PageInput):
    path: str | None = None
    symbol_id: str | None = Field(default=None, max_length=500)


class ReadLinesInput(ToolInput):
    path: str
    start_line: int = Field(ge=1, strict=True)
    end_line: int = Field(ge=1, strict=True)


class GitDiffInput(ToolInput):
    mode: Literal["working", "staged", "base"]
    base_ref: str | None = None
    paths: tuple[str, ...] = ()


class CandidateRangeInput(ToolInput):
    start_line: int = Field(ge=1, strict=True)
    end_line: int = Field(ge=1, strict=True)


class AddContextInput(ToolInput):
    path: str
    ranges: tuple[CandidateRangeInput, ...] = ()
    reason: str = Field(min_length=1, max_length=2_000)
    evidence: tuple[str, ...] = Field(default=(), max_length=50)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)


class RemoveContextInput(ToolInput):
    path: str
    reason: str = Field(min_length=1, max_length=2_000)


class FinalizeContextInput(ToolInput):
    summary: str = Field(min_length=1, max_length=10_000)
    unknowns: tuple[str, ...] = Field(default=(), max_length=100)
    completeness_claims: tuple[str, ...] = Field(default=(), max_length=100)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0, allow_inf_nan=False)


TOOL_INPUT_MODELS: dict[str, type[ToolInput]] = {
    "get_repository_overview": EmptyInput,
    "list_tree": ListTreeInput,
    "search_index": SearchIndexInput,
    "search_symbols": SearchSymbolsInput,
    "search_text": SearchTextInput,
    "get_file_summary": PathInput,
    "get_symbol_summary": SymbolInput,
    "find_imports": PagedPathInput,
    "find_importers": PagedPathInput,
    "find_references": PagedSymbolInput,
    "find_callers": PagedSymbolInput,
    "find_related_tests": RelatedTestsInput,
    "read_file": PathInput,
    "read_lines": ReadLinesInput,
    "get_git_diff": GitDiffInput,
    "add_to_context": AddContextInput,
    "remove_from_context": RemoveContextInput,
    "get_context_budget": EmptyInput,
    "finalize_context": FinalizeContextInput,
}

DISCOVERY_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    name: model.model_json_schema() for name, model in TOOL_INPUT_MODELS.items()
}


class GitDiffProvider(Protocol):
    """Trusted read-only adapter; discovery itself never invokes a process."""

    def get_diff(
        self,
        mode: Literal["working", "staged", "base"],
        *,
        base_ref: str | None,
        paths: tuple[str, ...],
        max_bytes: int,
    ) -> GitDiffResult:
        """Return an already-sanitized bounded diff without executing model input."""
        ...


@dataclass(frozen=True, slots=True)
class GitDiffResult:
    text: str
    touched_paths: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class DiscoveryKnowledge:
    """Pinned facts and mode-permitted interpretations used by tool queries."""

    snapshot: ProjectSnapshot
    mode: DiscoveryMode
    code_maps: Mapping[str, FileCodeMap]
    semantic_analyses: Mapping[str, FileSemanticAnalysis] = field(default_factory=dict)
    manifest: IndexManifest | None = None
    overview: RepositoryOverview | None = None
    architecture: ArchitectureMap | None = None
    features: FeatureMap | None = None
    stale_index_paths: tuple[str, ...] = ()
    index_unavailable_reason: str | None = None


class ToolBudgetExceededError(RuntimeError):
    """Raised before an operation would exceed an authoritative run budget."""


@dataclass(slots=True)
class ToolBudgetTracker:
    """Mutable internal accounting converted to a frozen public usage model."""

    limits: DiscoveryBudget
    steps: int = 0
    model_calls: int = 0
    files_read: int = 0
    source_bytes: int = 0
    tool_result_bytes: int = 0
    context_bytes: int = 0
    context_files: int = 0

    def usage(self) -> DiscoveryBudgetUsage:
        return DiscoveryBudgetUsage(
            steps=self.steps,
            model_calls=self.model_calls,
            files_read=self.files_read,
            source_bytes=self.source_bytes,
            tool_result_bytes=self.tool_result_bytes,
            context_bytes=self.context_bytes,
            context_files=self.context_files,
        )

    def charge_read(self, size_bytes: int) -> None:
        if self.files_read + 1 > self.limits.max_files_read:
            raise ToolBudgetExceededError("maximum files read exceeded")
        if self.source_bytes + size_bytes > self.limits.max_source_bytes:
            raise ToolBudgetExceededError("maximum source bytes read exceeded")
        self.files_read += 1
        self.source_bytes += size_bytes

    def charge_result(self, size_bytes: int) -> None:
        if self.tool_result_bytes + size_bytes > self.limits.max_tool_result_bytes:
            raise ToolBudgetExceededError("maximum tool-result bytes exceeded")
        self.tool_result_bytes += size_bytes


@dataclass(frozen=True, slots=True)
class _ToolResult:
    data: dict[str, Any]
    code: str = "ok"
    ok: bool = True
    truncated: bool = False
    made_progress: bool = False


class DiscoveryToolExecutor:
    """Engine-owned dispatcher exposing no filesystem handles or code execution."""

    def __init__(
        self,
        knowledge: DiscoveryKnowledge,
        budget: ToolBudgetTracker,
        *,
        pinned_paths: tuple[str, ...] = (),
        excluded_paths: tuple[str, ...] = (),
        git_diff_provider: GitDiffProvider | None = None,
    ) -> None:
        self.knowledge = knowledge
        self.budget = budget
        self._files = {item.path: item for item in knowledge.snapshot.files}
        self._excluded = frozenset(excluded_paths)
        self._pinned = frozenset(pinned_paths)
        self._selected: dict[str, DiscoveryCandidate] = {}
        self._removed: dict[str, str] = {}
        self._git_diff_provider = git_diff_provider
        self.read_paths: set[str] = set()
        self._cursors: dict[str, tuple[str, int]] = {}
        self._consumed_cursors: set[str] = set()
        self.last_git_diff: GitDiffResult | None = None
        for path in pinned_paths:
            project_file = self._require_path(path)
            self._selected[path] = _candidate(
                path,
                kind="full_file",
                reason="Manually pinned by the reviewer.",
                source="manual-pin",
                source_sha256=project_file.sha256,
                manually_pinned=True,
            )
        self._update_context_usage()

    @property
    def selected(self) -> tuple[DiscoveryCandidate, ...]:
        return tuple(self._selected[path] for path in sorted(self._selected))

    @property
    def removed(self) -> Mapping[str, str]:
        return dict(self._removed)

    def execute(
        self,
        *,
        step: int,
        action_id: str,
        tool_name: str,
        arguments: Mapping[str, Any],
    ) -> DiscoveryObservation:
        """Validate one closed input and return one bounded structured observation."""

        model = TOOL_INPUT_MODELS.get(tool_name)
        if model is None:
            return self._observation(
                step,
                action_id,
                tool_name,
                _ToolResult({}, code="unknown_tool", ok=False),
            )
        try:
            parsed = model.model_validate(dict(arguments))
            result = self._dispatch(tool_name, parsed)
        except ValidationError as exc:
            result = _ToolResult(
                {"message": _validation_message(exc)}, code="invalid_input", ok=False
            )
        except ValueError as exc:
            result = _ToolResult({"message": str(exc)}, code="invalid_input", ok=False)
        except ToolBudgetExceededError as exc:
            result = _ToolResult(
                {"message": str(exc)}, code="budget_exceeded", ok=False
            )
        except Exception:
            result = _ToolResult(
                {"message": "read-only tool operation failed"},
                code="internal_error",
                ok=False,
            )
        return self._observation(step, action_id, tool_name, result)

    def _dispatch(self, name: str, value: ToolInput) -> _ToolResult:
        handlers = {
            "get_repository_overview": self._overview,
            "list_tree": self._list_tree,
            "search_index": self._search_index,
            "search_symbols": self._search_symbols,
            "search_text": self._search_text,
            "get_file_summary": self._file_summary,
            "get_symbol_summary": self._symbol_summary,
            "find_imports": self._find_imports,
            "find_importers": self._find_importers,
            "find_references": self._find_references,
            "find_callers": self._find_callers,
            "find_related_tests": self._find_related_tests,
            "read_file": self._read_file,
            "read_lines": self._read_lines,
            "get_git_diff": self._git_diff,
            "add_to_context": self._add_context,
            "remove_from_context": self._remove_context,
            "get_context_budget": self._context_budget,
            "finalize_context": self._finalize_marker,
        }
        return handlers[name](value)

    def _overview(self, value: ToolInput) -> _ToolResult:
        del value
        snapshot = self.knowledge.snapshot
        languages = dict(sorted(snapshot.summary.languages.items()))
        data: dict[str, Any] = {
            "mode": self.knowledge.mode,
            "source_snapshot_digest": calculate_source_snapshot_digest(snapshot),
            "file_count": len(snapshot.files),
            "source_bytes": sum(item.size_bytes for item in snapshot.files),
            "languages": languages,
            "all_allowed_files_queryable": True,
            "index_generation_id": (
                self.knowledge.manifest.generation_id
                if self.knowledge.manifest is not None
                else None
            ),
            "indexed_current_files": len(self.knowledge.code_maps),
            "stale_index_paths": list(self.knowledge.stale_index_paths),
            "index_unavailable_reason": self.knowledge.index_unavailable_reason,
            "semantic_file_count": len(self.knowledge.semantic_analyses),
            "architecture_available": self.knowledge.architecture is not None,
            "features_available": self.knowledge.features is not None,
        }
        if self.knowledge.overview is not None:
            data["relationship_count"] = len(self.knowledge.overview.relationships)
            data["test_relationship_count"] = len(
                self.knowledge.overview.test_relationships
            )
            data["diagnostics"] = [
                item.model_dump(mode="json")
                for item in self.knowledge.overview.diagnostics[:20]
            ]
        return _ToolResult(data, made_progress=True)

    def _list_tree(self, raw: ToolInput) -> _ToolResult:
        value = _as(raw, ListTreeInput)
        prefix = "" if value.path is None else self._validate_directory(value.path)
        entries: set[tuple[str, str]] = set()
        base_depth = 0 if not prefix else prefix.count("/") + 1
        for path in sorted(self._files):
            if prefix and path != prefix and not path.startswith(prefix + "/"):
                continue
            parts = path.split("/")
            for index in range(1, len(parts)):
                directory = "/".join(parts[:index])
                depth = index - base_depth
                if depth <= value.depth and (not prefix or directory != prefix):
                    entries.add((directory, "directory"))
            file_depth = len(parts) - base_depth - 1
            if file_depth <= value.depth:
                entries.add((path, "file"))
        items = [{"path": path, "kind": kind} for path, kind in sorted(entries)]
        return self._page("list_tree", value, items, MAX_LIST_RESULT_BYTES)

    def _search_index(self, raw: ToolInput) -> _ToolResult:
        value = _as(raw, SearchIndexInput)
        if self.knowledge.mode is DiscoveryMode.FRESH:
            return _ToolResult(
                {"message": "persistent semantic index is disabled in fresh mode"},
                code="unavailable",
                ok=False,
            )
        query = value.query.casefold()
        allowed_kinds = set(value.kinds)
        hits: list[dict[str, Any]] = []
        for path, code_map in sorted(self.knowledge.code_maps.items()):
            fact_text = code_map.model_dump_json().casefold()
            if query in fact_text and (
                not allowed_kinds or "verified_fact" in allowed_kinds
            ):
                hits.append(
                    {
                        "kind": "verified_fact",
                        "path": path,
                        "parse_status": code_map.parse_status,
                        "symbols": [
                            item.qualified_name for item in code_map.symbols[:10]
                        ],
                    }
                )
            analysis = self.knowledge.semantic_analyses.get(path)
            if (
                analysis is not None
                and query in analysis.model_dump_json().casefold()
                and (not allowed_kinds or "model_interpretation" in allowed_kinds)
            ):
                hits.append(
                    {
                        "kind": "model_interpretation",
                        "path": path,
                        "primary_purpose": (
                            analysis.primary_purpose.claim
                            if analysis.primary_purpose is not None
                            else None
                        ),
                    }
                )
        for label, record in (
            ("architecture", self.knowledge.architecture),
            ("feature", self.knowledge.features),
        ):
            if (
                record is not None
                and query in record.model_dump_json().casefold()
                and (not allowed_kinds or "model_interpretation" in allowed_kinds)
            ):
                hits.append(
                    {
                        "kind": "model_interpretation",
                        "scope": label,
                        "record": record.model_dump(mode="json"),
                    }
                )
        return self._page("search_index", value, hits, MAX_SEARCH_RESULT_BYTES)

    def _search_symbols(self, raw: ToolInput) -> _ToolResult:
        value = _as(raw, SearchSymbolsInput)
        query = value.query.casefold()
        if value.path_prefix is not None:
            prefix = self._validate_directory(value.path_prefix)
        else:
            prefix = ""
        hits: list[dict[str, Any]] = []
        for path, code_map in sorted(self.knowledge.code_maps.items()):
            if prefix and path != prefix and not path.startswith(prefix + "/"):
                continue
            for symbol in code_map.symbols:
                if value.kinds and symbol.kind not in value.kinds:
                    continue
                haystack = " ".join(
                    item
                    for item in (
                        symbol.name,
                        symbol.qualified_name,
                        symbol.signature or "",
                    )
                    if item
                ).casefold()
                if query in haystack:
                    hits.append(_symbol_hit(path, symbol))
        return self._page("search_symbols", value, hits, MAX_SEARCH_RESULT_BYTES)

    def _search_text(self, raw: ToolInput) -> _ToolResult:
        value = _as(raw, SearchTextInput)
        if value.path_glob is not None:
            _validate_glob(value.path_glob)
        needle = value.query if value.case_sensitive else value.query.casefold()
        hits: list[dict[str, Any]] = []
        truncated = False
        for path, project_file in sorted(self._files.items()):
            if value.path_glob is not None and not fnmatch.fnmatchcase(
                path, value.path_glob
            ):
                continue
            if len(hits) >= MAX_RESULT_LIMIT:
                truncated = True
                break
            try:
                selected = self._read(project_file)
            except ToolBudgetExceededError:
                truncated = True
                break
            text = selected.blocks[0].text
            per_file = 0
            for line_number, line in enumerate(text.splitlines(), start=1):
                candidate = line if value.case_sensitive else line.casefold()
                if needle not in candidate:
                    continue
                hits.append(
                    {
                        "path": path,
                        "line": line_number,
                        "snippet": line[:500],
                        "source_sha256": project_file.sha256,
                    }
                )
                per_file += 1
                if per_file >= 20 or len(hits) >= MAX_RESULT_LIMIT:
                    truncated = True
                    break
        result = self._page("search_text", value, hits, MAX_SEARCH_RESULT_BYTES)
        return _ToolResult(
            result.data,
            code=result.code,
            ok=result.ok,
            truncated=result.truncated or truncated,
            made_progress=result.made_progress,
        )

    def _file_summary(self, raw: ToolInput) -> _ToolResult:
        value = _as(raw, PathInput)
        project_file = self._require_path(value.path)
        code_map = self.knowledge.code_maps.get(project_file.path)
        if code_map is None:
            return _ToolResult(
                {
                    "path": project_file.path,
                    "source_sha256": project_file.sha256,
                    "size_bytes": project_file.size_bytes,
                    "message": "no current structural record is available",
                },
                code="unavailable",
                ok=False,
            )
        data: dict[str, Any] = {
            "path": project_file.path,
            "source_sha256": project_file.sha256,
            "facts": code_map.model_dump(mode="json"),
            "interpretation": None,
        }
        analysis = self.knowledge.semantic_analyses.get(project_file.path)
        if analysis is not None:
            data["interpretation"] = analysis.model_dump(mode="json")
        return _bounded_data(data, MAX_SUMMARY_RESULT_BYTES)

    def _symbol_summary(self, raw: ToolInput) -> _ToolResult:
        value = _as(raw, SymbolInput)
        found = self._find_symbol(value.symbol_id)
        if found is None:
            return _ToolResult({}, code="not_found", ok=False)
        path, symbol = found
        data: dict[str, Any] = {"path": path, "fact": symbol.model_dump(mode="json")}
        analysis = self.knowledge.semantic_analyses.get(path)
        if analysis is not None:
            semantic = next(
                (
                    item
                    for item in analysis.symbols
                    if item.symbol_id == symbol.symbol_id
                ),
                None,
            )
            data["interpretation"] = (
                semantic.model_dump(mode="json") if semantic is not None else None
            )
        return _bounded_data(data, MAX_SUMMARY_RESULT_BYTES)

    def _find_imports(self, raw: ToolInput) -> _ToolResult:
        value = _as(raw, PagedPathInput)
        path = self._require_path(value.path).path
        code_map = self.knowledge.code_maps.get(path)
        if code_map is None:
            return _ToolResult({}, code="unavailable", ok=False)
        items = [item.model_dump(mode="json") for item in code_map.imports]
        return self._page("find_imports", value, items, MAX_SEARCH_RESULT_BYTES)

    def _find_importers(self, raw: ToolInput) -> _ToolResult:
        value = _as(raw, PagedPathInput)
        path = self._require_path(value.path).path
        items = [
            {"source_path": source_path, "import": item.model_dump(mode="json")}
            for source_path, code_map in sorted(self.knowledge.code_maps.items())
            for item in code_map.imports
            if item.target_file_path == path
        ]
        return self._page("find_importers", value, items, MAX_SEARCH_RESULT_BYTES)

    def _find_references(self, raw: ToolInput) -> _ToolResult:
        value = _as(raw, PagedSymbolInput)
        if self._find_symbol(value.symbol_id) is None:
            return _ToolResult({}, code="not_found", ok=False)
        items = [
            {
                "source_path": path,
                "relationship": relationship.model_dump(mode="json"),
            }
            for path, code_map in sorted(self.knowledge.code_maps.items())
            for relationship in code_map.relationships
            if relationship.target.symbol_id == value.symbol_id
        ]
        return self._page("find_references", value, items, MAX_SEARCH_RESULT_BYTES)

    def _find_callers(self, raw: ToolInput) -> _ToolResult:
        value = _as(raw, PagedSymbolInput)
        if self._find_symbol(value.symbol_id) is None:
            return _ToolResult({}, code="not_found", ok=False)
        items = [
            {
                "source_path": path,
                "source_symbol_id": symbol.symbol_id,
                "call": call.model_dump(mode="json"),
            }
            for path, code_map in sorted(self.knowledge.code_maps.items())
            for symbol in code_map.symbols
            for call in symbol.direct_calls
            if call.target_symbol_id == value.symbol_id
        ]
        unresolved = sum(
            call.resolution == "unresolved"
            for code_map in self.knowledge.code_maps.values()
            for symbol in code_map.symbols
            for call in symbol.direct_calls
        )
        result = self._page("find_callers", value, items, MAX_SEARCH_RESULT_BYTES)
        return _ToolResult(
            {**result.data, "unresolved_calls_in_repository": unresolved},
            truncated=result.truncated,
            made_progress=result.made_progress,
        )

    def _find_related_tests(self, raw: ToolInput) -> _ToolResult:
        value = _as(raw, RelatedTestsInput)
        if value.path is None and value.symbol_id is None:
            raise ValueError("path or symbol_id is required")
        path = None
        if value.path is not None:
            path = self._require_path(value.path).path
        if value.symbol_id is not None:
            found = self._find_symbol(value.symbol_id)
            if found is None:
                return _ToolResult({}, code="not_found", ok=False)
            symbol_path = found[0]
            if path is not None and path != symbol_path:
                raise ValueError("path does not own symbol_id")
            path = symbol_path
        assert path is not None
        related: dict[str, dict[str, Any]] = {}
        for source_path, code_map in self.knowledge.code_maps.items():
            for relationship in code_map.relationships:
                target = relationship.target.file_path
                if (
                    relationship.kind in {"tests", "tested_by"}
                    and source_path == path
                    and target
                ):
                    related[target] = {
                        "path": target,
                        "basis": relationship.detection_method,
                        "relationship_id": relationship.relationship_id,
                    }
                elif relationship.kind == "tests" and target == path:
                    related[source_path] = {
                        "path": source_path,
                        "basis": relationship.detection_method,
                        "relationship_id": relationship.relationship_id,
                    }
        if self.knowledge.overview is not None:
            for test_relationship in self.knowledge.overview.test_relationships:
                if test_relationship.source_file == path:
                    related[test_relationship.test_file] = {
                        "path": test_relationship.test_file,
                        "basis": test_relationship.detection_method,
                        "relationship_id": test_relationship.relationship_id,
                    }
                elif test_relationship.test_file == path:
                    related[test_relationship.source_file] = {
                        "path": test_relationship.source_file,
                        "basis": test_relationship.detection_method,
                        "relationship_id": test_relationship.relationship_id,
                    }
        return self._page(
            "find_related_tests",
            value,
            [related[key] for key in sorted(related)],
            MAX_SEARCH_RESULT_BYTES,
        )

    def _read_file(self, raw: ToolInput) -> _ToolResult:
        value = _as(raw, PathInput)
        project_file = self._require_path(value.path)
        if project_file.size_bytes > MAX_READ_FILE_BYTES:
            return _ToolResult(
                {"message": "file exceeds read_file per-call limit; use read_lines"},
                code="limit_exceeded",
                ok=False,
            )
        selected = self._read(project_file)
        return _ToolResult(
            {
                "path": project_file.path,
                "source_sha256": project_file.sha256,
                "text": selected.blocks[0].text,
                "line_count": selected.source_line_count,
                "freshness": "verified_current_source",
            },
            made_progress=True,
        )

    def _read_lines(self, raw: ToolInput) -> _ToolResult:
        value = _as(raw, ReadLinesInput)
        if value.end_line < value.start_line:
            raise ValueError("end_line must not precede start_line")
        if value.end_line - value.start_line + 1 > MAX_READ_LINES:
            return _ToolResult(
                {"message": "requested range exceeds 500 lines"},
                code="limit_exceeded",
                ok=False,
            )
        project_file = self._require_path(value.path)
        self.budget.charge_read(project_file.size_bytes)
        self.read_paths.add(project_file.path)
        selected = read_selected_text_file(
            self.knowledge.snapshot,
            project_file,
            line_ranges=(LineRange(value.start_line, value.end_line),),
            limits=ReaderLimits(
                max_files=1,
                max_source_bytes=max(project_file.size_bytes, 1),
                max_content_bytes=MAX_READ_LINES_BYTES,
            ),
        )
        block = selected.blocks[0]
        return _ToolResult(
            {
                "path": project_file.path,
                "source_sha256": project_file.sha256,
                "start_line": value.start_line,
                "end_line": value.end_line,
                "text": block.text,
                "freshness": "verified_current_source",
            },
            made_progress=True,
        )

    def _git_diff(self, raw: ToolInput) -> _ToolResult:
        value = _as(raw, GitDiffInput)
        if value.mode == "base":
            if (
                value.base_ref is None
                or not _SAFE_REF.fullmatch(value.base_ref)
                or value.base_ref.startswith("-")
            ):
                raise ValueError("base mode requires a safe bounded base_ref")
        elif value.base_ref is not None:
            raise ValueError("base_ref is accepted only in base mode")
        paths = tuple(self._require_path(path).path for path in value.paths)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("diff paths must be unique and canonical")
        if self._git_diff_provider is None:
            return _ToolResult(
                {"message": "no trusted read-only Git diff provider was supplied"},
                code="unavailable",
                ok=False,
            )
        result = self._git_diff_provider.get_diff(
            value.mode,
            base_ref=value.base_ref,
            paths=paths,
            max_bytes=MAX_GIT_DIFF_BYTES,
        )
        encoded = result.text.encode("utf-8")
        if len(encoded) > MAX_GIT_DIFF_BYTES:
            raise ValueError("Git diff provider returned an oversized result")
        touched = tuple(self._require_path(path).path for path in result.touched_paths)
        if touched != tuple(sorted(set(touched))):
            raise ValueError("Git diff touched paths must be unique and canonical")
        for path in result.deleted_paths:
            validate_portable_relative_path(path)
        self.last_git_diff = result
        return _ToolResult(
            {
                "mode": value.mode,
                "text": result.text,
                "touched_paths": list(touched),
                "deleted_paths": list(result.deleted_paths),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            },
            truncated=result.truncated,
            made_progress=True,
        )

    def _add_context(self, raw: ToolInput) -> _ToolResult:
        value = _as(raw, AddContextInput)
        project_file = self._require_path(value.path)
        if project_file.path in self._excluded:
            return _ToolResult(
                {"message": "manual exclusion has precedence"},
                code="not_allowed",
                ok=False,
            )
        ranges = _canonical_ranges(value.ranges)
        code_map = self.knowledge.code_maps.get(project_file.path)
        if (
            ranges
            and code_map is not None
            and ranges[-1].end_line > code_map.line_count
        ):
            raise ValueError("selected line range exceeds verified source line count")
        kind: Literal["full_file", "line_ranges", "related_test"]
        if ranges:
            kind = "line_ranges"
        elif _looks_like_test_path(project_file.path):
            kind = "related_test"
        else:
            kind = "full_file"
        candidate = _candidate(
            project_file.path,
            kind=kind,
            ranges=ranges,
            reason=value.reason,
            source="model-tool:add_to_context",
            evidence=value.evidence,
            confidence=value.confidence,
            source_sha256=project_file.sha256,
            manually_pinned=project_file.path in self._pinned,
            model_selected=True,
        )
        previous = self._selected.get(project_file.path)
        if previous is not None and previous.manually_pinned and ranges:
            return _ToolResult(
                {"message": "manual full-file pin cannot be silently narrowed"},
                code="not_allowed",
                ok=False,
            )
        self._selected[project_file.path] = candidate
        self._removed.pop(project_file.path, None)
        try:
            self._update_context_usage()
        except ToolBudgetExceededError:
            if previous is None:
                del self._selected[project_file.path]
            else:
                self._selected[project_file.path] = previous
            self._update_context_usage()
            raise
        changed = previous != candidate
        return _ToolResult(
            {"candidate": candidate.model_dump(mode="json")},
            made_progress=changed,
        )

    def _remove_context(self, raw: ToolInput) -> _ToolResult:
        value = _as(raw, RemoveContextInput)
        path = self._require_path(value.path).path
        previous = self._selected.get(path)
        if previous is None:
            return _ToolResult({}, code="not_found", ok=False)
        if previous.manually_pinned:
            return _ToolResult(
                {"message": "manual pinned includes cannot be removed by the model"},
                code="not_allowed",
                ok=False,
            )
        del self._selected[path]
        self._removed[path] = value.reason
        self._update_context_usage()
        return _ToolResult({"removed_path": path}, made_progress=True)

    def _context_budget(self, value: ToolInput) -> _ToolResult:
        del value
        usage = self.budget.usage()
        limits = self.budget.limits
        return _ToolResult(
            {
                "used": usage.model_dump(mode="json"),
                "remaining": {
                    "steps": limits.max_steps - usage.steps,
                    "model_calls": limits.max_model_calls - usage.model_calls,
                    "files_read": limits.max_files_read - usage.files_read,
                    "source_bytes": limits.max_source_bytes - usage.source_bytes,
                    "tool_result_bytes": limits.max_tool_result_bytes
                    - usage.tool_result_bytes,
                    "context_bytes": limits.max_context_bytes - usage.context_bytes,
                    "context_files": limits.max_context_files - usage.context_files,
                },
                "selected_paths": sorted(self._selected),
            },
            made_progress=False,
        )

    def _finalize_marker(self, raw: ToolInput) -> _ToolResult:
        value = _as(raw, FinalizeContextInput)
        return _ToolResult(
            {"finalize_request": value.model_dump(mode="json")}, made_progress=True
        )

    def _read(self, project_file: ProjectFile) -> SelectedTextFile:
        self.budget.charge_read(project_file.size_bytes)
        self.read_paths.add(project_file.path)
        return read_selected_text_file(
            self.knowledge.snapshot,
            project_file,
            limits=ReaderLimits(
                max_files=1,
                max_source_bytes=max(project_file.size_bytes, 1),
                max_content_bytes=max(project_file.size_bytes, 1),
            ),
        )

    def _require_path(self, path: str) -> ProjectFile:
        portable = validate_portable_relative_path(path)
        project_file = self._files.get(portable)
        if project_file is None:
            raise ValueError("path is not an allowed ProjectSnapshot file")
        if project_file.is_text is not True:
            raise ValueError("path is not selectable text")
        return project_file

    def _validate_directory(self, path: str) -> str:
        portable = validate_portable_relative_path(path)
        if portable not in self._files and not any(
            candidate.startswith(portable + "/") for candidate in self._files
        ):
            raise ValueError("tree path is absent from the allowed snapshot")
        return portable

    def _find_symbol(self, symbol_id: str) -> tuple[str, SymbolRecord] | None:
        for path, code_map in sorted(self.knowledge.code_maps.items()):
            for symbol in code_map.symbols:
                if symbol.symbol_id == symbol_id:
                    return path, symbol
        return None

    def _page(
        self,
        tool_name: str,
        value: PageInput,
        items: list[dict[str, Any]],
        maximum_bytes: int,
    ) -> _ToolResult:
        query_key = hashlib.sha256(
            _json_bytes(
                {
                    "tool": tool_name,
                    "input": value.model_dump(mode="json", exclude={"cursor"}),
                }
            )
        ).hexdigest()
        offset = 0
        if value.cursor is not None:
            if value.cursor in self._consumed_cursors:
                return _ToolResult(
                    {"message": "pagination cursor was already consumed"},
                    code="invalid_input",
                    ok=False,
                )
            cursor_state = self._cursors.pop(value.cursor, None)
            if cursor_state is None or cursor_state[0] != query_key:
                return _ToolResult(
                    {"message": "pagination cursor is invalid for this query"},
                    code="invalid_input",
                    ok=False,
                )
            self._consumed_cursors.add(value.cursor)
            offset = cursor_state[1]
        page = items[offset : offset + value.limit]
        truncated = offset + len(page) < len(items)
        while page and len(_json_bytes({"items": page})) > maximum_bytes:
            page.pop()
            truncated = True
        next_cursor = None
        if truncated and page:
            next_offset = offset + len(page)
            next_cursor = hashlib.sha256(
                f"{query_key}:{next_offset}".encode()
            ).hexdigest()
            self._cursors[next_cursor] = (query_key, next_offset)
        return _ToolResult(
            {
                "items": page,
                "total_matches": len(items),
                "next_cursor": next_cursor,
            },
            truncated=truncated,
            made_progress=bool(page),
        )

    def _observation(
        self,
        step: int,
        action_id: str,
        tool_name: str,
        result: _ToolResult,
    ) -> DiscoveryObservation:
        data = result.data
        encoded = _json_bytes(data)
        try:
            self.budget.charge_result(len(encoded))
        except ToolBudgetExceededError as exc:
            data = {"message": str(exc)}
            encoded = _json_bytes(data)
            result = _ToolResult(data, code="budget_exceeded", ok=False)
        return DiscoveryObservation(
            step=step,
            action_id=action_id,
            tool_name=tool_name,
            ok=result.ok,
            code=result.code,
            data=data,
            truncated=result.truncated,
            result_bytes=len(encoded),
            made_progress=result.made_progress,
        )

    def _update_context_usage(self) -> None:
        files = len(self._selected)
        # Full-file raw bytes are a safe upper bound. Ranged content is charged
        # authoritatively after its verified canonical final read; charging the
        # entire source here would incorrectly reject a narrow range in a large file.
        size = sum(
            self._files[path].size_bytes
            for path, candidate in self._selected.items()
            if not candidate.ranges
        )
        if files > self.budget.limits.max_context_files:
            raise ToolBudgetExceededError("maximum context files exceeded")
        if size > self.budget.limits.max_context_bytes:
            raise ToolBudgetExceededError("maximum context bytes exceeded")
        self.budget.context_files = files
        self.budget.context_bytes = size


def _as[InputType: ToolInput](value: ToolInput, expected: type[InputType]) -> InputType:
    if not isinstance(value, expected):
        raise TypeError("validated tool input type mismatch")
    return value


def _candidate(
    path: str,
    *,
    kind: Literal["full_file", "line_ranges", "related_test"],
    reason: str,
    source: str,
    source_sha256: str,
    ranges: tuple[DiscoveryLineRange, ...] = (),
    evidence: tuple[str, ...] = (),
    confidence: float | None = None,
    manually_pinned: bool = False,
    model_selected: bool = False,
    added_by_completeness: bool = False,
) -> DiscoveryCandidate:
    key = hashlib.sha256(
        _json_bytes([path, kind, [item.model_dump(mode="json") for item in ranges]])
    ).hexdigest()[:32]
    return DiscoveryCandidate(
        candidate_id=f"candidate:{key}",
        kind=kind,
        path=path,
        ranges=ranges,
        reason=SelectionReason(
            summary=reason,
            discovery_source=source,
            evidence=evidence,
        ),
        confidence=confidence,
        source_sha256=source_sha256,
        manually_pinned=manually_pinned,
        model_selected=model_selected,
        added_by_completeness=added_by_completeness,
    )


def _canonical_ranges(
    values: Iterable[CandidateRangeInput],
) -> tuple[DiscoveryLineRange, ...]:
    ordered = sorted(
        (
            DiscoveryLineRange(start_line=item.start_line, end_line=item.end_line)
            for item in values
        ),
        key=lambda item: (item.start_line, item.end_line),
    )
    merged: list[DiscoveryLineRange] = []
    for item in ordered:
        if not merged or item.start_line > merged[-1].end_line + 1:
            merged.append(item)
        else:
            previous = merged[-1]
            merged[-1] = DiscoveryLineRange(
                start_line=previous.start_line,
                end_line=max(previous.end_line, item.end_line),
            )
    return tuple(merged)


def _symbol_hit(path: str, symbol: SymbolRecord) -> dict[str, Any]:
    return {
        "path": path,
        "symbol_id": symbol.symbol_id,
        "name": symbol.name,
        "qualified_name": symbol.qualified_name,
        "kind": symbol.kind,
        "signature": symbol.signature,
        "declaration_range": symbol.declaration_range.model_dump(mode="json"),
    }


def _looks_like_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return (
        path.lower().startswith(("test/", "tests/"))
        or name.startswith("test_")
        or name.endswith(("_test.py", ".test.js", ".spec.js", ".test.ts", ".spec.ts"))
    )


def _bounded_data(data: dict[str, Any], maximum: int) -> _ToolResult:
    if len(_json_bytes(data)) <= maximum:
        return _ToolResult(data, made_progress=True)
    return _ToolResult(
        {"message": "summary exceeds the per-call result limit"},
        code="limit_exceeded",
        ok=False,
        truncated=True,
    )


def _validate_glob(value: str) -> None:
    if (
        not value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or "\\" in value
        or value.startswith("/")
        or re.match(r"^[A-Za-z]:", value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("path_glob must be a safe portable relative glob")
    PurePosixPath("placeholder").match(value)


def _validation_message(error: ValidationError) -> str:
    first = error.errors(include_url=False)[0]
    location = ".".join(str(item) for item in first["loc"])
    return f"invalid tool input at {location or '<root>'}: {first['msg']}"


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


__all__ = [
    "DISCOVERY_TOOL_SCHEMAS",
    "DiscoveryKnowledge",
    "DiscoveryToolExecutor",
    "FinalizeContextInput",
    "GitDiffProvider",
    "GitDiffResult",
    "TOOL_INPUT_MODELS",
    "ToolBudgetExceededError",
    "ToolBudgetTracker",
]
