"""Small explicit CodeMap extractor mapping; this is not a plugin system."""

from __future__ import annotations

from collections.abc import Callable

from contextforge.intelligence.codemap import FileCodeMap
from contextforge.intelligence.fallback import extract_fallback_code_map
from contextforge.intelligence.python import (
    DEFAULT_CODEMAP_SOURCE_LIMIT,
    extract_python_code_map,
)
from contextforge.repositories import ProjectFile, ProjectSnapshot

CodeMapExtractor = Callable[..., FileCodeMap]
SUPPORTED_CODEMAP_LANGUAGES = ("Python",)
_EXTRACTORS: dict[str, CodeMapExtractor] = {"Python": extract_python_code_map}


def extract_code_map(
    snapshot: ProjectSnapshot,
    project_file: ProjectFile,
    *,
    max_source_bytes: int = DEFAULT_CODEMAP_SOURCE_LIMIT,
) -> FileCodeMap:
    """Dispatch one snapshot-owned file to Python or the verified fallback."""

    extractor = _EXTRACTORS.get(project_file.language or "", extract_fallback_code_map)
    return extractor(
        snapshot,
        project_file,
        max_source_bytes=max_source_bytes,
    )


def extract_code_maps(
    snapshot: ProjectSnapshot,
    *,
    max_source_bytes: int = DEFAULT_CODEMAP_SOURCE_LIMIT,
) -> tuple[FileCodeMap, ...]:
    """Extract and cross-resolve every selectable snapshot file canonically."""

    from contextforge.intelligence.relationships import resolve_relationships

    maps = tuple(
        extract_code_map(snapshot, project_file, max_source_bytes=max_source_bytes)
        for project_file in sorted(snapshot.files, key=lambda item: item.path)
    )
    return resolve_relationships(maps)


__all__ = [
    "SUPPORTED_CODEMAP_LANGUAGES",
    "CodeMapExtractor",
    "extract_code_map",
    "extract_code_maps",
]
