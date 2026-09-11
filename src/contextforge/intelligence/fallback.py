"""Verified file-level structure placeholder for readable generic text."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from contextforge.context.reader import ReaderLimits, read_selected_text_file
from contextforge.intelligence.codemap import (
    FileCodeMap,
    ParserDiagnostic,
    configuration_key_digest,
)
from contextforge.intelligence.models import AnalyzerIdentity
from contextforge.intelligence.python import DEFAULT_CODEMAP_SOURCE_LIMIT
from contextforge.repositories import ProjectFile, ProjectSnapshot

FALLBACK_ANALYZER = AnalyzerIdentity(
    analyzer_id="generic-text-structure",
    analyzer_version="3",
    analysis_prompt_version="none",
    response_schema_version=1,
)


def extract_fallback_code_map(
    snapshot: ProjectSnapshot,
    project_file: ProjectFile,
    *,
    max_source_bytes: int = DEFAULT_CODEMAP_SOURCE_LIMIT,
) -> FileCodeMap:
    """Verify readable text and emit no invented structural declarations."""

    selected = read_selected_text_file(
        snapshot,
        project_file,
        limits=ReaderLimits(
            max_files=1,
            max_source_bytes=max_source_bytes,
            max_content_bytes=max_source_bytes,
        ),
    )
    language = project_file.language or "unknown"
    configuration_key_digests = _configuration_key_digests(
        project_file.path, selected.blocks[0].text
    )
    return FileCodeMap(
        path=project_file.path,
        source_sha256=project_file.sha256,
        source_size_bytes=project_file.size_bytes,
        language=project_file.language,
        analyzer=FALLBACK_ANALYZER,
        parse_status="unsupported",
        line_count=selected.source_line_count,
        configuration_key_digests=configuration_key_digests,
        diagnostics=(
            ParserDiagnostic(
                code="no_structural_extractor",
                message=f"no rich structural extractor is available for {language}",
                severity="info",
            ),
        ),
    )


_CONFIG_SUFFIXES = {".env", ".ini", ".json", ".toml", ".yaml", ".yml"}
_CONFIG_NAMES = {
    "dockerfile",
    "makefile",
    "package.json",
    "pyproject.toml",
    "settings",
}
_KEY_PATTERN = re.compile(
    r"""(?mx)
    ^\s*["']?([A-Za-z][A-Za-z0-9_.-]{1,127})["']?\s*[:=]
    |["']([A-Z][A-Z0-9_]{1,127})["']
    """
)


def _configuration_key_digests(path: str, source: str) -> tuple[str, ...]:
    pure = PurePosixPath(path.casefold())
    if pure.suffix not in _CONFIG_SUFFIXES and pure.name not in _CONFIG_NAMES:
        return ()
    values = {
        value
        for match in _KEY_PATTERN.finditer(source)
        for value in match.groups()
        if value is not None
    }
    return tuple(sorted(configuration_key_digest(value) for value in values))


__all__ = ["FALLBACK_ANALYZER", "extract_fallback_code_map"]
