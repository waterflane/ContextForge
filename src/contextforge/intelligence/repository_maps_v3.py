"""Deterministic repository maps aggregated from CodeMaps and grounded cards."""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal

from pydantic import field_validator, model_validator

from contextforge.intelligence.codemap import FileCodeMap
from contextforge.intelligence.models import (
    IndexModel,
    Sha256,
    validate_portable_relative_path,
)

if TYPE_CHECKING:
    from contextforge.intelligence.cards import SemanticCard

REPOSITORY_MAP_SCHEMA_VERSION: Literal[3] = 3
RepositoryMapKind = Literal["architecture", "conventions", "features"]


class RepositoryMapEntry(IndexModel):
    """One deterministic map group with canonical member paths."""

    name: str
    paths: tuple[str, ...]
    attributes: dict[str, str | int | float | bool]

    @field_validator("paths")
    @classmethod
    def validate_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        validated = tuple(validate_portable_relative_path(path) for path in value)
        if validated != tuple(sorted(set(validated))):
            raise ValueError("repository map paths must be unique and canonical")
        return validated


class RepositoryMap(IndexModel):
    """One model-free repository aggregation."""

    schema_version: Literal[3] = REPOSITORY_MAP_SCHEMA_VERSION
    record_kind: Literal["repository_map"] = "repository_map"
    map_kind: RepositoryMapKind
    source_snapshot_digest: Sha256
    entries: tuple[RepositoryMapEntry, ...]

    @model_validator(mode="after")
    def validate_entries(self) -> RepositoryMap:
        names = tuple(item.name for item in self.entries)
        if names != tuple(sorted(set(names))):
            raise ValueError("repository map entries must be unique and canonical")
        return self


def build_repository_maps_v3(
    code_maps: tuple[FileCodeMap, ...],
    cards: tuple[SemanticCard, ...],
    source_snapshot_digest: str,
) -> tuple[RepositoryMap, RepositoryMap, RepositoryMap]:
    """Build architecture, conventions, and features without provider calls."""

    architecture_groups: dict[str, list[FileCodeMap]] = defaultdict(list)
    for code_map in code_maps:
        parent = PurePosixPath(code_map.path).parent.as_posix()
        architecture_groups["root" if parent == "." else parent].append(code_map)
    architecture = RepositoryMap(
        map_kind="architecture",
        source_snapshot_digest=source_snapshot_digest,
        entries=tuple(
            RepositoryMapEntry(
                name=name,
                paths=tuple(sorted(item.path for item in items)),
                attributes={
                    "files": len(items),
                    "symbols": sum(len(item.symbols) for item in items),
                    "imports": sum(len(item.imports) for item in items),
                },
            )
            for name, items in sorted(architecture_groups.items())
        ),
    )

    suffixes = Counter(
        PurePosixPath(item.path).suffix.casefold() or "(none)" for item in code_maps
    )
    tests = tuple(sorted(item.path for item in code_maps if _is_test(item.path)))
    configs = tuple(sorted(card.path for card in cards if card.profile == "config"))
    convention_entries = [
        RepositoryMapEntry(
            name=f"extension:{suffix}",
            paths=tuple(
                sorted(
                    item.path
                    for item in code_maps
                    if (PurePosixPath(item.path).suffix.casefold() or "(none)")
                    == suffix
                )
            ),
            attributes={"count": count},
        )
        for suffix, count in sorted(suffixes.items())
    ]
    if tests:
        convention_entries.append(
            RepositoryMapEntry(
                name="test-layout", paths=tests, attributes={"count": len(tests)}
            )
        )
    if configs:
        convention_entries.append(
            RepositoryMapEntry(
                name="configuration-layout",
                paths=configs,
                attributes={"count": len(configs)},
            )
        )
    conventions = RepositoryMap(
        map_kind="conventions",
        source_snapshot_digest=source_snapshot_digest,
        entries=tuple(sorted(convention_entries, key=lambda item: item.name)),
    )

    feature_groups: dict[str, list[SemanticCard]] = defaultdict(list)
    for card in cards:
        first = PurePosixPath(card.path).parts[0]
        feature_groups[first].append(card)
    features = RepositoryMap(
        map_kind="features",
        source_snapshot_digest=source_snapshot_digest,
        entries=tuple(
            RepositoryMapEntry(
                name=name,
                paths=tuple(sorted(card.path for card in items)),
                attributes={
                    "cards": len(items),
                    "grounded_concepts": sum(len(card.concepts) for card in items),
                },
            )
            for name, items in sorted(feature_groups.items())
        ),
    )
    return architecture, conventions, features


def _is_test(path: str) -> bool:
    pure = PurePosixPath(path)
    return any(
        part.casefold() in {"test", "tests", "spec", "specs"} for part in pure.parts
    ) or pure.name.casefold().startswith(("test_", "spec_"))


__all__ = [
    "REPOSITORY_MAP_SCHEMA_VERSION",
    "RepositoryMap",
    "RepositoryMapEntry",
    "RepositoryMapKind",
    "build_repository_maps_v3",
]
