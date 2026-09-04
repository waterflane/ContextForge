"""Capability reporting independent of the number of observed relationships."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypedDict

from .codemap import FileCodeMap
from .polyglot import POLYGLOT_ANALYZER, SUPPORTED_POLYGLOT_LANGUAGES
from .python import PYTHON_ANALYZER


class RelationshipCoverage(TypedDict):
    status: str
    scope: str
    file_counts: dict[str, int]
    limitations: list[str]


def relationship_coverage(
    maps: Mapping[str, FileCodeMap],
    source_paths: tuple[str, ...],
) -> RelationshipCoverage:
    """Describe static extraction coverage, never dynamic graph completeness."""
    groups: dict[str, int] = {
        "supported": 0,
        "partial": 0,
        "unsupported": 0,
        "unknown": 0,
    }
    for path in sorted(source_paths):
        code_map = maps.get(path)
        status = "unknown"
        if code_map is not None:
            if code_map.language == "Python" and code_map.analyzer == PYTHON_ANALYZER:
                status = "supported" if code_map.parse_status == "parsed" else "partial"
            elif (
                code_map.language in SUPPORTED_POLYGLOT_LANGUAGES
                and code_map.analyzer == POLYGLOT_ANALYZER
            ):
                status = "unsupported"
        groups[status] += 1
    statuses = {key for key, count in groups.items() if count}
    status = next(iter(statuses)) if len(statuses) == 1 else "partial"
    return {
        "status": status if statuses else "unknown",
        "scope": "static calls and imports in current repository source files",
        "file_counts": groups,
        "limitations": [
            "Only Python static calls and imports are extracted and resolved.",
            "TypeScript and other polyglot calls/imports are not indexed.",
            "Zero matches does not establish absence of dynamic "
            "or unindexed references.",
            "Unresolved counts include only observed calls.",
        ],
    }
