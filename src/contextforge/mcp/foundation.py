"""Bounded read-only MCP tools composed from public ContextForge APIs."""

from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from contextforge.application import build_discovery_request
from contextforge.context import (
    ContextBudget,
    ContextBuildOptions,
    ContextCompilerError,
    ContextSelection,
    LineRange,
    LineRangeRequest,
    build_context_package,
    compile_context_capsule,
    inspect_context_package_json,
)
from contextforge.discovery import (
    DISCOVERY_TOOL_SCHEMAS,
    DiscoveryBudget,
    DiscoveryMode,
    DiscoveryRequest,
    DiscoverySession,
    GitDiffProvider,
    GitDiffResult,
)
from contextforge.git import GitDiffRequest, collect_git_diff
from contextforge.intelligence import (
    REPOSITORY_MAP_KINDS,
    IndexManifest,
    IndexManifestNotFoundError,
    IndexManifestReadError,
    SourceRange,
    load_architecture_map,
    load_feature_map,
    load_file_code_map,
    load_manifest,
    load_orientation_map,
    load_relationship_graph,
    load_repository_map_v3,
    retrieve_context_candidates,
)
from contextforge.models import ModelProvider
from contextforge.repositories import ProjectSnapshot, scan_repository

MCP_MAX_RESULT_BYTES = 2 * 1024 * 1024


class _ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _SuggestInput(_ToolInput):
    task: str = Field(min_length=1, max_length=20_000)
    discovery: Literal["indexed", "fresh", "hybrid"] = "hybrid"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_files: int = Field(default=100, ge=1, le=1_000, strict=True)
    max_context_bytes: int = Field(
        default=1_000_000, ge=1, le=10 * 1024 * 1024, strict=True
    )


class _PackageRange(_ToolInput):
    path: str
    start_line: int = Field(ge=1, strict=True)
    end_line: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def validate_order(self) -> _PackageRange:
        if self.end_line < self.start_line:
            raise ValueError("end_line must not precede start_line")
        return self


class _BuildPackageInput(_ToolInput):
    task: str = Field(default="MCP context package", min_length=1, max_length=20_000)
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    ranges: tuple[_PackageRange, ...] = ()
    include_tree: bool = True
    max_files: int = Field(default=100, ge=1, le=1_000, strict=True)
    max_context_bytes: int = Field(
        default=1_000_000, ge=1, le=10 * 1024 * 1024, strict=True
    )


class _InspectPackageInput(_ToolInput):
    package_json: str = Field(min_length=1, max_length=16 * 1024 * 1024)


class _MapInput(_ToolInput):
    include_graph: bool = False


class _SearchInput(_ToolInput):
    task: str = Field(min_length=1, max_length=20_000)
    working_files: tuple[str, ...] = ()
    diff_paths: tuple[str, ...] = ()
    limit: int = Field(default=20, ge=1, le=1_000, strict=True)
    rerank: bool = False


class _SymbolInput(_ToolInput):
    query: str = Field(min_length=1, max_length=1_000)
    limit: int = Field(default=50, ge=1, le=1_000, strict=True)


class _CapsuleRange(_ToolInput):
    path: str
    start_line: int = Field(ge=1, strict=True)
    end_line: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def validate_order(self) -> _CapsuleRange:
        if self.end_line < self.start_line:
            raise ValueError("end_line must not precede start_line")
        return self


class _CompileInput(_SearchInput):
    working_lines: tuple[_CapsuleRange, ...] = ()
    pinned_full_files: tuple[str, ...] = ()
    context_window_tokens: int = Field(default=32_768, ge=1, strict=True)
    history_tokens: int = Field(default=0, ge=0, strict=True)
    response_tokens: int = Field(default=4_096, ge=0, strict=True)
    safety_margin_tokens: int = Field(default=1_024, ge=0, strict=True)
    git_diff: str | None = Field(default=None, max_length=2 * 1024 * 1024)


_QUERY_TOOL_MAP = {
    "repository_overview": "get_repository_overview",
    "list_tree": "list_tree",
    "search_index": "search_index",
    "search_symbols": "search_symbols",
    "search_text": "search_text",
    "get_file_summary": "get_file_summary",
    "get_symbol_summary": "get_symbol_summary",
    "find_imports": "find_imports",
    "find_importers": "find_importers",
    "find_references": "find_references",
    "find_related_tests": "find_related_tests",
    "read_file": "read_file",
    "read_lines": "read_lines",
    "get_git_diff": "get_git_diff",
}

MCP_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    public: DISCOVERY_TOOL_SCHEMAS[internal]
    for public, internal in _QUERY_TOOL_MAP.items()
}
MCP_TOOL_SCHEMAS.update(
    {
        "suggest_context": _SuggestInput.model_json_schema(),
        "build_context_package": _BuildPackageInput.model_json_schema(),
        "inspect_context_package": _InspectPackageInput.model_json_schema(),
        "map": _MapInput.model_json_schema(),
        "search": _SearchInput.model_json_schema(),
        "symbol": _SymbolInput.model_json_schema(),
        "compile": _CompileInput.model_json_schema(),
    }
)

_TOOL_DESCRIPTIONS = {
    "repository_overview": "Return snapshot and pinned-index coverage metadata.",
    "list_tree": "List bounded allowed snapshot tree entries.",
    "search_index": "Search labelled verified facts and model interpretations.",
    "search_symbols": "Search verified symbol names, signatures, and ranges.",
    "search_text": "Search current verified source text with bounded snippets.",
    "get_file_summary": "Return current structural and permitted semantic file data.",
    "get_symbol_summary": "Return one verified symbol and permitted interpretation.",
    "find_imports": "Return verified imports from one allowed file.",
    "find_importers": "Return verified reverse imports for one allowed file.",
    "find_references": "Return bounded static references to a verified symbol.",
    "find_related_tests": "Return best-effort structural source/test relationships.",
    "read_file": "Read one current verified allowed file within 256 KiB.",
    "read_lines": "Read at most 500 verified lines and 128 KiB.",
    "get_git_diff": "Collect a fixed-argument bounded read-only Git diff.",
    "suggest_context": (
        "Run bounded existing discovery and return a reviewable selection."
    ),
    "build_context_package": (
        "Build an in-memory verified ContextPackage; writes nothing."
    ),
    "inspect_context_package": (
        "Validate portable package JSON without repository access."
    ),
    "map": "Return the pinned Index v3 orientation and optional relationship graph.",
    "search": "Retrieve deterministic BM25/graph CandidateCards from Index v3.",
    "symbol": "Find verified exact or qualified symbols in the pinned generation.",
    "compile": "Compile a hard-budgeted Context Capsule v2 without writing source.",
}


class ReadOnlyToolError(RuntimeError):
    """One safe MCP tool error without tracebacks or absolute paths."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class _GitProvider(GitDiffProvider):
    snapshot: ProjectSnapshot

    def get_diff(
        self,
        mode: Literal["working", "staged", "base"],
        *,
        base_ref: str | None,
        paths: tuple[str, ...],
        max_bytes: int,
    ) -> GitDiffResult:
        request = GitDiffRequest(
            mode=mode,
            base_ref=base_ref,
            paths=paths,
            max_bytes=max_bytes,
        )
        result = collect_git_diff(self.snapshot, request)
        return GitDiffResult(
            text=result.text,
            touched_paths=result.touched_paths,
            deleted_paths=result.deleted_paths,
            truncated=result.truncated,
        )


class ReadOnlyMCPFoundation:
    """One root/snapshot/index-pinned collection of read-only tools."""

    def __init__(
        self,
        repository_root: str | Path,
        *,
        provider: ModelProvider | None = None,
        discovery_mode: DiscoveryMode = DiscoveryMode.HYBRID,
    ) -> None:
        self.snapshot = scan_repository(repository_root)
        self.provider = provider
        self.discovery_mode = discovery_mode
        self.git_provider = _GitProvider(self.snapshot)
        self._manifest: IndexManifest | None = None
        with suppress(IndexManifestNotFoundError):
            self._manifest = load_manifest(self.snapshot.root)
        request = DiscoveryRequest(
            task="Read-only MCP repository session",
            mode=discovery_mode,
            budget=DiscoveryBudget(
                max_steps=100,
                max_model_calls=20,
                max_files_read=1_000,
                max_source_bytes=16 * 1024 * 1024,
                max_tool_result_bytes=16 * 1024 * 1024,
                max_context_bytes=10 * 1024 * 1024,
                max_context_files=1_000,
                timeout_seconds=900,
            ),
        )
        # Knowledge/tool preparation never calls the provider. A provider is only
        # required later for the explicitly named suggest_context tool.
        session = DiscoverySession(
            self.snapshot,
            provider,
            request,
            git_diff_provider=self.git_provider,
        )
        self._executor, self.warnings = session.prepare_read_only_tools()
        self._step = 0

    @property
    def tool_descriptors(self) -> tuple[dict[str, object], ...]:
        """Return stable MCP tool declarations; no mutation capability is present."""

        return tuple(
            {
                "name": name,
                "description": _TOOL_DESCRIPTIONS[name],
                "inputSchema": MCP_TOOL_SCHEMAS[name],
                "annotations": {
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": name
                    not in {"suggest_context", "search", "compile"},
                    "openWorldHint": False,
                },
            }
            for name in MCP_TOOL_SCHEMAS
        )

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Execute one approved read-only tool and enforce a final result cap."""

        values = {} if arguments is None else arguments
        if name in _QUERY_TOOL_MAP:
            self._step += 1
            observation = self._executor.execute(
                step=self._step,
                action_id=f"mcp-{self._step}",
                tool_name=_QUERY_TOOL_MAP[name],
                arguments=values,
            )
            if not observation.ok:
                message = str(observation.data.get("message", observation.code))
                raise ReadOnlyToolError(observation.code, message)
            result = dict(observation.data)
            result.setdefault("truncated", observation.truncated)
        elif name == "suggest_context":
            result = await self._suggest(values)
        elif name == "build_context_package":
            result = self._build_package(values)
        elif name == "inspect_context_package":
            result = self._inspect_package(values)
        elif name == "map":
            result = self._map(values)
        elif name == "search":
            result = await self._search(values)
        elif name == "symbol":
            result = self._symbol(values)
        elif name == "compile":
            result = await self._compile(values)
        else:
            raise ReadOnlyToolError("unknown_tool", "unknown read-only MCP tool")
        _require_result_limit(result)
        return result

    def list_resources(self) -> tuple[dict[str, object], ...]:
        """Return immutable/read-only resource descriptors for the pinned session."""

        return (
            {
                "uri": "contextforge://repository/overview",
                "name": "Repository overview",
                "mimeType": "application/json",
            },
            {
                "uri": "contextforge://index/manifest",
                "name": "Pinned index manifest",
                "mimeType": "application/json",
            },
            {
                "uri": "contextforge://architecture",
                "name": "Pinned architecture interpretation",
                "mimeType": "application/json",
            },
            {
                "uri": "contextforge://features",
                "name": "Pinned feature interpretation",
                "mimeType": "application/json",
            },
        )

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """Read one bounded pinned resource without changing external state."""

        if uri == "contextforge://repository/overview":
            return await self.call_tool("repository_overview", {})
        if uri == "contextforge://index/manifest":
            if self._manifest is None:
                raise ReadOnlyToolError("unavailable", "no pinned index manifest")
            result = self._manifest.model_dump(mode="json")
        elif uri == "contextforge://architecture":
            if self._manifest is None:
                raise ReadOnlyToolError("unavailable", "no pinned architecture map")
            try:
                result = (
                    load_repository_map_v3(
                        self.snapshot.root,
                        "architecture",
                        manifest=self._manifest,
                    )
                    if self._manifest.schema_version == 3
                    else load_architecture_map(
                        self.snapshot.root, manifest=self._manifest
                    )
                ).model_dump(mode="json")
            except (IndexManifestReadError, ValueError) as exc:
                raise ReadOnlyToolError(
                    "unavailable", "no pinned architecture map"
                ) from exc
        elif uri == "contextforge://features":
            if self._manifest is None:
                raise ReadOnlyToolError("unavailable", "no pinned feature map")
            try:
                result = (
                    load_repository_map_v3(
                        self.snapshot.root, "features", manifest=self._manifest
                    )
                    if self._manifest.schema_version == 3
                    else load_feature_map(self.snapshot.root, manifest=self._manifest)
                ).model_dump(mode="json")
            except (IndexManifestReadError, ValueError) as exc:
                raise ReadOnlyToolError("unavailable", "no pinned feature map") from exc
        else:
            raise ReadOnlyToolError("not_found", "unknown MCP resource URI")
        _require_result_limit(result)
        return result

    async def _suggest(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            value = _SuggestInput.model_validate(arguments)
        except ValidationError as exc:
            raise ReadOnlyToolError("invalid_input", _validation_message(exc)) from exc
        if self.provider is None:
            raise ReadOnlyToolError(
                "unavailable", "suggest_context requires a configured server provider"
            )
        request = build_discovery_request(
            task=value.task,
            mode=value.discovery,
            includes=value.include,
            excludes=value.exclude,
            max_files=value.max_files,
            max_context_bytes=value.max_context_bytes,
        )
        run = await DiscoverySession(
            self.snapshot,
            self.provider,
            request,
            git_diff_provider=self.git_provider,
        ).run()
        final = run.final_selection
        if final is None:
            raise ReadOnlyToolError("unavailable", "discovery returned no selection")
        return final.model_dump(mode="json")

    def _build_package(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            value = _BuildPackageInput.model_validate(arguments)
            selection = ContextSelection(
                exact_paths=value.include,
                exclusions=value.exclude,
                line_ranges=tuple(
                    LineRangeRequest(
                        path=item.path,
                        range=LineRange(item.start_line, item.end_line),
                    )
                    for item in value.ranges
                ),
            )
            package = build_context_package(
                self.snapshot,
                ContextBuildOptions(
                    title=value.task,
                    selection=selection,
                    include_tree=value.include_tree,
                    max_files=value.max_files,
                    max_total_content_bytes=value.max_context_bytes,
                ),
            )
        except (ValidationError, ValueError, OSError) as exc:
            raise ReadOnlyToolError("invalid_input", _safe_error(exc)) from exc
        return package.model_dump(mode="json")

    def _inspect_package(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            value = _InspectPackageInput.model_validate(arguments)
            package, inspection = inspect_context_package_json(value.package_json)
        except (ValidationError, ValueError) as exc:
            raise ReadOnlyToolError("invalid_input", _safe_error(exc)) from exc
        return {
            "package": package.model_dump(mode="json"),
            "inspection": inspection.model_dump(mode="json"),
        }

    def _require_manifest(self) -> IndexManifest:
        if self._manifest is None:
            raise ReadOnlyToolError("unavailable", "no pinned Index v3 generation")
        if self._manifest.schema_version != 3:
            raise ReadOnlyToolError("unavailable", "pinned index requires a v3 rebuild")
        return self._manifest

    def _map(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            value = _MapInput.model_validate(arguments)
            manifest = self._require_manifest()
            result: dict[str, Any] = {
                "generation_id": manifest.generation_id,
                "orientation": load_orientation_map(
                    self.snapshot.root, manifest=manifest
                ).model_dump(mode="json"),
                "repository_maps": {
                    kind: load_repository_map_v3(
                        self.snapshot.root, kind, manifest=manifest
                    ).model_dump(mode="json")
                    for kind in REPOSITORY_MAP_KINDS
                    if getattr(manifest.artifacts, f"{kind}_map") is not None
                },
            }
            if value.include_graph:
                result["relationship_graph"] = load_relationship_graph(
                    self.snapshot.root, manifest=manifest
                ).model_dump(mode="json")
            return result
        except (ValidationError, ValueError, OSError) as exc:
            raise ReadOnlyToolError("invalid_input", _safe_error(exc)) from exc

    async def _search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            value = _SearchInput.model_validate(arguments)
            manifest = self._require_manifest()
            if value.rerank and self.provider is None:
                raise ReadOnlyToolError(
                    "unavailable", "rerank requires a configured server provider"
                )
            result = await retrieve_context_candidates(
                self.snapshot.root,
                value.task,
                manifest=manifest,
                working_set=value.working_files,
                diff_paths=value.diff_paths,
                limit=value.limit,
                provider=self.provider,
                rerank=value.rerank,
            )
            return result.model_dump(mode="json")
        except ReadOnlyToolError:
            raise
        except (ValidationError, ValueError, OSError) as exc:
            raise ReadOnlyToolError("invalid_input", _safe_error(exc)) from exc

    def _symbol(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            value = _SymbolInput.model_validate(arguments)
            manifest = self._require_manifest()
            query = value.query.strip().casefold()
            matches: list[tuple[int, str, str, dict[str, Any]]] = []
            for state in manifest.files:
                code_map = load_file_code_map(
                    self.snapshot.root, state.path, manifest=manifest
                )
                for symbol in code_map.symbols:
                    name = symbol.name.casefold()
                    qualified = symbol.qualified_name.casefold()
                    if query not in {name, qualified} and query not in qualified:
                        continue
                    rank = 0 if query == qualified else 1 if query == name else 2
                    matches.append(
                        (
                            rank,
                            qualified,
                            state.path,
                            {"path": state.path, **symbol.model_dump(mode="json")},
                        )
                    )
            return {
                "generation_id": manifest.generation_id,
                "query": value.query,
                "symbols": [
                    item[3]
                    for item in sorted(
                        matches, key=lambda match: (match[0], match[1], match[2])
                    )[: value.limit]
                ],
            }
        except (ValidationError, ValueError, OSError) as exc:
            raise ReadOnlyToolError("invalid_input", _safe_error(exc)) from exc

    async def _compile(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            value = _CompileInput.model_validate(arguments)
            manifest = self._require_manifest()
            if value.rerank and self.provider is None:
                raise ReadOnlyToolError(
                    "unavailable", "rerank requires a configured server provider"
                )
            working = tuple(
                sorted(
                    {
                        *value.working_files,
                        *(item.path for item in value.working_lines),
                        *value.pinned_full_files,
                    }
                )
            )
            retrieval = await retrieve_context_candidates(
                self.snapshot.root,
                value.task,
                manifest=manifest,
                working_set=working,
                diff_paths=value.diff_paths,
                limit=value.limit,
                provider=self.provider,
                rerank=value.rerank,
            )
            ranges: dict[str, list[SourceRange]] = {}
            for item in value.working_lines:
                ranges.setdefault(item.path, []).append(
                    SourceRange(
                        start_line=item.start_line,
                        start_column=0,
                        end_line=item.end_line,
                        end_column=0,
                    )
                )
            compiled = compile_context_capsule(
                self.snapshot.root,
                value.task,
                retrieval,
                budget=ContextBudget(
                    context_window_tokens=value.context_window_tokens,
                    history_tokens=value.history_tokens,
                    response_tokens=value.response_tokens,
                    safety_margin_tokens=value.safety_margin_tokens,
                ),
                manifest=manifest,
                working_files=working,
                working_lines={key: tuple(items) for key, items in ranges.items()},
                pinned_full_files=value.pinned_full_files,
                git_diff=value.git_diff,
            )
            return compiled.model_dump(mode="json")
        except ReadOnlyToolError:
            raise
        except (ContextCompilerError, ValidationError, ValueError, OSError) as exc:
            raise ReadOnlyToolError("invalid_input", _safe_error(exc)) from exc


def _require_result_limit(result: dict[str, Any]) -> None:
    try:
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ReadOnlyToolError(
            "internal_error", "tool result is not valid JSON"
        ) from exc
    if len(encoded) > MCP_MAX_RESULT_BYTES:
        raise ReadOnlyToolError("limit_exceeded", "tool result exceeds MCP byte limit")


def _validation_message(error: ValidationError) -> str:
    first = error.errors(include_url=False)[0]
    location = ".".join(str(item) for item in first["loc"])
    return f"invalid {location or 'input'}: {first['msg']}"


def _safe_error(error: BaseException) -> str:
    message = str(error).splitlines()[0]
    return message[:500] or type(error).__name__


__all__ = [
    "MCP_MAX_RESULT_BYTES",
    "MCP_TOOL_SCHEMAS",
    "ReadOnlyMCPFoundation",
    "ReadOnlyToolError",
]
