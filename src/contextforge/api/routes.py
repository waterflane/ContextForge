"""Read-only local development API routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from contextforge._metadata import APP_NAME, __version__
from contextforge.context import (
    ContextBudget,
    ContextCompilerError,
    compile_context_capsule,
)
from contextforge.core import HealthStatus, VersionInfo
from contextforge.intelligence import (
    SourceRange,
    load_file_code_map,
    load_manifest,
    load_orientation_map,
    load_relationship_graph,
    retrieve_context_candidates,
)

router = APIRouter()


class _ReadOnlyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository_root: str = Field(min_length=1, max_length=32_768)


class MapRequest(_ReadOnlyRequest):
    include_graph: bool = False


class SearchRequest(_ReadOnlyRequest):
    task: str = Field(min_length=1, max_length=20_000)
    working_files: tuple[str, ...] = ()
    diff_paths: tuple[str, ...] = ()
    limit: int = Field(default=20, ge=1, le=1_000)


class SymbolRequest(_ReadOnlyRequest):
    query: str = Field(min_length=1, max_length=1_000)
    limit: int = Field(default=50, ge=1, le=1_000)


class ContextRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_order(self) -> ContextRange:
        if self.end_line < self.start_line:
            raise ValueError("end_line must not precede start_line")
        return self


class CompileRequest(SearchRequest):
    working_lines: tuple[ContextRange, ...] = ()
    pinned_full_files: tuple[str, ...] = ()
    context_window_tokens: int = Field(default=32_768, ge=1)
    history_tokens: int = Field(default=0, ge=0)
    response_tokens: int = Field(default=4_096, ge=0)
    safety_margin_tokens: int = Field(default=1_024, ge=0)
    git_diff: str | None = Field(default=None, max_length=2 * 1024 * 1024)


@router.get("/health", response_model=HealthStatus)
def health() -> HealthStatus:
    """Return local API health status."""

    return HealthStatus(status="ok")


@router.get("/version", response_model=VersionInfo)
def version() -> VersionInfo:
    """Return application version information."""

    return VersionInfo(name=APP_NAME, version=__version__)


@router.post("/v1/map")
def repository_map(request: MapRequest) -> dict[str, object]:
    """Return pinned deterministic repository orientation and optional graph."""

    try:
        root = _root(request.repository_root)
        manifest = load_manifest(root)
        result: dict[str, object] = {
            "generation_id": manifest.generation_id,
            "orientation": load_orientation_map(root, manifest=manifest).model_dump(
                mode="json"
            ),
        }
        if request.include_graph:
            result["relationship_graph"] = load_relationship_graph(
                root, manifest=manifest
            ).model_dump(mode="json")
        return result
    except (OSError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.post("/v1/search")
async def search(request: SearchRequest) -> dict[str, object]:
    """Return deterministic CandidateCards; the HTTP API performs no model calls."""

    try:
        result = await retrieve_context_candidates(
            _root(request.repository_root),
            request.task,
            working_set=request.working_files,
            diff_paths=request.diff_paths,
            limit=request.limit,
        )
        return result.model_dump(mode="json")
    except (OSError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.post("/v1/symbol")
def symbol(request: SymbolRequest) -> dict[str, object]:
    """Search verified symbols without reading arbitrary source ranges."""

    try:
        root = _root(request.repository_root)
        manifest = load_manifest(root)
        query = request.query.strip().casefold()
        matches: list[tuple[int, str, str, dict[str, object]]] = []
        for state in manifest.files:
            code_map = load_file_code_map(root, state.path, manifest=manifest)
            for item in code_map.symbols:
                name = item.name.casefold()
                qualified = item.qualified_name.casefold()
                if query not in {name, qualified} and query not in qualified:
                    continue
                rank = 0 if query == qualified else 1 if query == name else 2
                matches.append(
                    (
                        rank,
                        qualified,
                        state.path,
                        {"path": state.path, **item.model_dump(mode="json")},
                    )
                )
        return {
            "generation_id": manifest.generation_id,
            "query": request.query,
            "symbols": [
                item[3]
                for item in sorted(
                    matches, key=lambda value: (value[0], value[1], value[2])
                )[: request.limit]
            ],
        }
    except (OSError, ValueError) as exc:
        raise _http_error(exc) from exc


@router.post("/v1/compile")
async def compile_capsule(request: CompileRequest) -> dict[str, object]:
    """Compile a source-read-only, generation-pinned Context Capsule v2."""

    try:
        root = _root(request.repository_root)
        manifest = load_manifest(root)
        working = tuple(
            sorted(
                {
                    *request.working_files,
                    *(item.path for item in request.working_lines),
                    *request.pinned_full_files,
                }
            )
        )
        retrieval = await retrieve_context_candidates(
            root,
            request.task,
            manifest=manifest,
            working_set=working,
            diff_paths=request.diff_paths,
            limit=request.limit,
        )
        grouped: dict[str, list[SourceRange]] = {}
        for item in request.working_lines:
            grouped.setdefault(item.path, []).append(
                SourceRange(
                    start_line=item.start_line,
                    start_column=0,
                    end_line=item.end_line,
                    end_column=0,
                )
            )
        compiled = compile_context_capsule(
            root,
            request.task,
            retrieval,
            budget=ContextBudget(
                context_window_tokens=request.context_window_tokens,
                history_tokens=request.history_tokens,
                response_tokens=request.response_tokens,
                safety_margin_tokens=request.safety_margin_tokens,
            ),
            manifest=manifest,
            working_files=working,
            working_lines={key: tuple(value) for key, value in grouped.items()},
            pinned_full_files=request.pinned_full_files,
            git_diff=request.git_diff,
        )
        return compiled.model_dump(mode="json")
    except (ContextCompilerError, OSError, ValueError) as exc:
        raise _http_error(exc) from exc


def _root(value: str) -> Path:
    return Path(value).expanduser().resolve(strict=True)


def _http_error(error: BaseException) -> HTTPException:
    message = str(error).splitlines()[0][:500] or type(error).__name__
    return HTTPException(status_code=400, detail=message)


__all__ = [
    "CompileRequest",
    "ContextRange",
    "MapRequest",
    "SearchRequest",
    "SymbolRequest",
    "router",
]
