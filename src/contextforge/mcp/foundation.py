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
    ContextBuildOptions,
    ContextSelection,
    LineRange,
    LineRangeRequest,
    build_context_package,
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
    IndexManifest,
    IndexManifestNotFoundError,
    IndexManifestReadError,
    load_architecture_map,
    load_feature_map,
    load_manifest,
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
            provider,  # type: ignore[arg-type]
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
                    "idempotentHint": name not in {"suggest_context"},
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
                result = load_architecture_map(
                    self.snapshot.root, manifest=self._manifest
                ).model_dump(mode="json")
            except IndexManifestReadError as exc:
                raise ReadOnlyToolError(
                    "unavailable", "no pinned architecture map"
                ) from exc
        elif uri == "contextforge://features":
            if self._manifest is None:
                raise ReadOnlyToolError("unavailable", "no pinned feature map")
            try:
                result = load_feature_map(
                    self.snapshot.root, manifest=self._manifest
                ).model_dump(mode="json")
            except IndexManifestReadError as exc:
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
