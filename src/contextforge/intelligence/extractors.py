"""Small explicit CodeMap extractor mapping; this is not a plugin system."""

from __future__ import annotations

from collections.abc import Callable

from contextforge.context import ReaderLimits, read_selected_text_file
from contextforge.intelligence.chunks import plan_source_chunks
from contextforge.intelligence.codemap import FileCodeMap
from contextforge.intelligence.fallback import extract_fallback_code_map
from contextforge.intelligence.polyglot import (
    SUPPORTED_POLYGLOT_LANGUAGES,
    extract_polyglot_code_map,
)
from contextforge.intelligence.python import (
    DEFAULT_CODEMAP_SOURCE_LIMIT,
    extract_python_code_map,
)
from contextforge.repositories import ProjectFile, ProjectSnapshot

CodeMapExtractor = Callable[..., FileCodeMap]
SUPPORTED_CODEMAP_LANGUAGES = ("Python", *SUPPORTED_POLYGLOT_LANGUAGES)
_EXTRACTORS: dict[str, CodeMapExtractor] = {
    "Python": extract_python_code_map,
    **{
        language: extract_polyglot_code_map for language in SUPPORTED_POLYGLOT_LANGUAGES
    },
}


def extract_code_map(
    snapshot: ProjectSnapshot,
    project_file: ProjectFile,
    *,
    max_source_bytes: int = DEFAULT_CODEMAP_SOURCE_LIMIT,
) -> FileCodeMap:
    """Dispatch one snapshot-owned file to Python or the verified fallback."""

    extractor = _EXTRACTORS.get(project_file.language or "", extract_fallback_code_map)
    code_map = extractor(
        snapshot,
        project_file,
        max_source_bytes=max_source_bytes,
    )
    selected = read_selected_text_file(
        snapshot,
        project_file,
        limits=ReaderLimits(
            max_files=1,
            max_source_bytes=max_source_bytes,
            max_content_bytes=max_source_bytes,
        ),
    )
    chunks, truncated = plan_source_chunks(selected.blocks[0].text, code_map)
    return code_map.model_copy(
        update={
            "source_regions": tuple(chunk.source_range for chunk in chunks),
            "source_regions_truncated": truncated,
        }
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
