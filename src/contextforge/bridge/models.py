"""Closed parameter models for the persistent JSON-RPC bridge."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contextforge.core.validation import Sha256, validate_portable_relative_path
from contextforge.discovery import (
    DiscoveryBudget,
    DiscoveryBudgetUsage,
    DiscoveryLineRange,
    DiscoveryMode,
)


class BridgeParams(BaseModel):
    """Strict base for all method parameters."""

    model_config = ConfigDict(extra="forbid", strict=True)

    timeout_ms: int | None = Field(default=None, ge=1, le=900_000, strict=True)


class HelloParams(BridgeParams):
    protocol_version: str = Field(
        min_length=1, max_length=32, pattern=r"^[0-9]+\.[0-9]+$"
    )
    client_name: str | None = Field(default=None, min_length=1, max_length=200)


class StatusParams(BridgeParams):
    expected_snapshot_digest: Sha256 | None = None


class SnapshotParams(BridgeParams):
    pass


class IndexParams(BridgeParams):
    """Bridge 2 parameters for one atomic tracked index job."""

    action: Literal["build", "update"]
    expected_snapshot_digest: Sha256
    provider: str | None = Field(default=None, min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    base_url: str | None = Field(default=None, min_length=1, max_length=2_000)
    concurrency: int | None = Field(default=None, ge=1, le=8, strict=True)
    request_timeout: float | None = Field(default=None, ge=1, le=600)
    operation_timeout: float | None = Field(default=None, ge=1, le=86_400)
    context_window: int | None = Field(
        default=None, ge=1_024, le=2_000_000, strict=True
    )
    json_repair_attempts: int | None = Field(default=None, ge=0, le=10, strict=True)
    max_output_tokens: int | None = Field(default=None, ge=96, le=32_768, strict=True)
    fail_on_error: bool = False
    fail_fast: bool = False
    max_failures: int | None = Field(default=None, ge=1, strict=True)
    force_reanalyze: bool = False
    max_files: int | None = Field(default=None, ge=1, strict=True)
    semantic_scope: Literal["priority", "all", "none"] | None = None
    semantic_max_requests: int | None = Field(default=None, ge=1, strict=True)
    semantic_max_input_tokens: int | None = Field(default=None, ge=1, strict=True)
    semantic_max_chunks_per_file: int | None = Field(
        default=None, ge=1, le=4, strict=True
    )
    local_only: bool = False
    recover_stale_lock: bool = False
    confirm_unknown_lock: bool = False

    @model_validator(mode="after")
    def validate_failure_policy(self) -> IndexParams:
        if self.fail_fast and self.max_failures is not None:
            raise ValueError("fail_fast and max_failures cannot be used together")
        return self


class DiscoverParams(BridgeParams):
    expected_snapshot_digest: Sha256
    task: str = Field(min_length=1, max_length=20_000)
    mode: DiscoveryMode = DiscoveryMode.HYBRID
    pinned_paths: tuple[str, ...] = ()
    excluded_paths: tuple[str, ...] = ()
    strict: bool = False
    budget: DiscoveryBudget = Field(default_factory=DiscoveryBudget)

    @field_validator("mode", mode="before")
    @classmethod
    def validate_mode(cls, value: object) -> object:
        if isinstance(value, str):
            return DiscoveryMode(value)
        return value

    @field_validator("pinned_paths", "excluded_paths")
    @classmethod
    def validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        paths = tuple(validate_portable_relative_path(path) for path in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("paths must be unique and in canonical order")
        return paths


ExpansionOperation = Literal[
    "symbol",
    "text",
    "callers",
    "importers",
    "related_tests",
]


class ExpandParams(BridgeParams):
    expected_snapshot_digest: Sha256
    preparation_id: Sha256
    operation: ExpansionOperation
    arguments: dict[str, Any] = Field(default_factory=dict)
    budget_usage: DiscoveryBudgetUsage = Field(default_factory=DiscoveryBudgetUsage)


class BridgeSelectionItem(BaseModel):
    """One candidate selection with optional identity assertions."""

    model_config = ConfigDict(extra="forbid", strict=True)

    candidate_id: str = Field(min_length=1, max_length=200)
    path: str | None = None
    source_sha256: Sha256 | None = None
    ranges: tuple[DiscoveryLineRange, ...] = ()

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        return None if value is None else validate_portable_relative_path(value)

    @model_validator(mode="after")
    def validate_ranges(self) -> BridgeSelectionItem:
        previous_end = 0
        for item in self.ranges:
            if item.start_line <= previous_end:
                raise ValueError("ranges must be sorted and disjoint")
            previous_end = item.end_line
        return self


class ReadParams(BridgeParams):
    expected_snapshot_digest: Sha256
    preparation_id: Sha256
    items: tuple[BridgeSelectionItem, ...] = Field(min_length=1)

    @field_validator("items")
    @classmethod
    def validate_items(
        cls, value: tuple[BridgeSelectionItem, ...]
    ) -> tuple[BridgeSelectionItem, ...]:
        identifiers = tuple(item.candidate_id for item in value)
        if identifiers != tuple(sorted(set(identifiers))):
            raise ValueError("selection items must be unique and in canonical order")
        return value


class PackageParams(ReadParams):
    include_tree: bool = True


class CancelParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str | int

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str | int) -> str | int:
        if isinstance(value, bool) or (
            isinstance(value, str) and (not value or len(value) > 200)
        ):
            raise ValueError("id must be a bounded non-empty string or integer")
        return value


class ShutdownParams(BridgeParams):
    pass


class MapParams(StatusParams):
    """Bridge 2.1 generation-pinned orientation-map request."""

    expected_snapshot_digest: Sha256


class SearchParams(BridgeParams):
    """Bridge 2.1 deterministic retrieval with optional bounded reranking."""

    expected_snapshot_digest: Sha256
    task: str = Field(min_length=1, max_length=20_000)
    working_files: tuple[str, ...] = ()
    diff_paths: tuple[str, ...] = ()
    limit: int = Field(default=20, ge=1, le=1_000, strict=True)
    rerank: bool = False
    provider: str | None = Field(default=None, min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    base_url: str | None = Field(default=None, min_length=1, max_length=2_000)
    request_timeout: float | None = Field(default=None, ge=1, le=600)

    @field_validator("working_files", "diff_paths")
    @classmethod
    def validate_search_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        paths = tuple(validate_portable_relative_path(path) for path in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("search paths must be unique and canonical")
        return paths


class SymbolParams(BridgeParams):
    """Bridge 2.1 exact/qualified symbol lookup."""

    expected_snapshot_digest: Sha256
    query: str = Field(min_length=1, max_length=1_000)
    limit: int = Field(default=50, ge=1, le=1_000, strict=True)


class CompileRange(BaseModel):
    """One exact Working Set source interval."""

    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    start_line: int = Field(ge=1, strict=True)
    end_line: int = Field(ge=1, strict=True)

    @field_validator("path")
    @classmethod
    def validate_compile_path(cls, value: str) -> str:
        return validate_portable_relative_path(value)

    @model_validator(mode="after")
    def validate_compile_range(self) -> CompileRange:
        if self.end_line < self.start_line:
            raise ValueError("end_line must not precede start_line")
        return self


class CompileParams(SearchParams):
    """Bridge 2.1 retrieval and Context Capsule v2 compilation request."""

    working_lines: tuple[CompileRange, ...] = ()
    pinned_full_files: tuple[str, ...] = ()
    context_window_tokens: int = Field(default=32_768, ge=1, strict=True)
    history_tokens: int = Field(default=0, ge=0, strict=True)
    response_tokens: int = Field(default=4_096, ge=0, strict=True)
    safety_margin_tokens: int = Field(default=1_024, ge=0, strict=True)
    git_diff: str | None = Field(default=None, max_length=2 * 1024 * 1024)

    @field_validator("pinned_full_files")
    @classmethod
    def validate_full_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        paths = tuple(validate_portable_relative_path(path) for path in value)
        if paths != tuple(sorted(set(paths))):
            raise ValueError("pinned full paths must be unique and canonical")
        return paths


__all__ = [
    "BridgeParams",
    "BridgeSelectionItem",
    "CancelParams",
    "CompileParams",
    "CompileRange",
    "DiscoverParams",
    "ExpandParams",
    "ExpansionOperation",
    "HelloParams",
    "IndexParams",
    "MapParams",
    "PackageParams",
    "ReadParams",
    "SearchParams",
    "ShutdownParams",
    "SnapshotParams",
    "StatusParams",
    "SymbolParams",
]
