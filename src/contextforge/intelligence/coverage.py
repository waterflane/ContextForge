"""Capability reporting independent of the number of observed relationships."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TypedDict

from contextforge.repositories import ProjectFile

from .codemap import FileCodeMap
from .fallback import FALLBACK_ANALYZER
from .polyglot import POLYGLOT_ANALYZER, SUPPORTED_POLYGLOT_LANGUAGES
from .python import PYTHON_ANALYZER

RELATIONSHIP_SOURCE_LANGUAGES = frozenset(
    {
        "Batch",
        "C",
        "C#",
        "C++",
        "Dockerfile",
        "Go",
        "Java",
        "JavaScript",
        "Kotlin",
        "Makefile",
        "PHP",
        "PowerShell",
        "Python",
        "Ruby",
        "Rust",
        "SQL",
        "Shell",
        "Swift",
        "TypeScript",
    }
)


class RelationshipCoverage(TypedDict):
    status: str
    scope: str
    file_counts: dict[str, int]
    limitations: list[str]


def relationship_source_paths(files: Iterable[ProjectFile]) -> tuple[str, ...]:
    """Return recognized source files relevant to caller/import coverage."""

    return tuple(
        item.path
        for item in files
        if item.is_text and item.language in RELATIONSHIP_SOURCE_LANGUAGES
    )


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
            ) or (
                code_map.language in RELATIONSHIP_SOURCE_LANGUAGES
                and code_map.analyzer == FALLBACK_ANALYZER
                and code_map.parse_status == "unsupported"
            ):
                status = "unsupported"
        groups[status] += 1
    statuses = {key for key, count in groups.items() if count}
    status = next(iter(statuses)) if len(statuses) == 1 else "partial"
    return {
        "status": status if statuses else "unknown",
        "scope": "recognized programming-language files in the current repository",
        "file_counts": groups,
        "limitations": [
            "Only Python static calls and imports are extracted and resolved.",
            "TypeScript and other polyglot calls/imports are not indexed.",
            "Recognized source languages without relationship extraction are counted "
            "as unsupported.",
            "Zero matches does not establish absence of dynamic "
            "or unindexed references.",
            "Unresolved counts include only observed calls.",
        ],
    }


__all__ = [
    "RELATIONSHIP_SOURCE_LANGUAGES",
    "RelationshipCoverage",
    "relationship_coverage",
    "relationship_source_paths",
]
