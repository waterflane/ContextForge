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
    protocol_version: Literal["1.0"] = "1.0"
    client_name: str | None = Field(default=None, min_length=1, max_length=200)


class StatusParams(BridgeParams):
    expected_snapshot_digest: Sha256 | None = None


class SnapshotParams(BridgeParams):
    pass


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
    action_id: str | None = Field(default=None, min_length=1, max_length=200)


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


class CancelParams(BridgeParams):
    id: str | int

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str | int) -> str | int:
        if isinstance(value, bool) or (isinstance(value, str) and not value):
            raise ValueError("id must be a non-empty string or integer")
        return value


class ShutdownParams(BridgeParams):
    pass


__all__ = [
    "BridgeParams",
    "BridgeSelectionItem",
    "CancelParams",
    "DiscoverParams",
    "ExpandParams",
    "ExpansionOperation",
    "HelloParams",
    "PackageParams",
    "ReadParams",
    "ShutdownParams",
    "SnapshotParams",
    "StatusParams",
]
