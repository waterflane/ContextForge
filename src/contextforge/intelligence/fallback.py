"""Verified file-level fallback for languages without a structural extractor."""

from __future__ import annotations

from contextforge.context import ReaderLimits, read_selected_text_file
from contextforge.intelligence.codemap import FileCodeMap, ParserDiagnostic
from contextforge.intelligence.models import AnalyzerIdentity
from contextforge.intelligence.python import DEFAULT_CODEMAP_SOURCE_LIMIT
from contextforge.repositories import ProjectFile, ProjectSnapshot

FALLBACK_ANALYZER = AnalyzerIdentity(
    analyzer_id="unsupported-language-fallback",
    analyzer_version="1",
    analysis_prompt_version="none",
    response_schema_version=1,
)


def extract_fallback_code_map(
    snapshot: ProjectSnapshot,
    project_file: ProjectFile,
    *,
    max_source_bytes: int = DEFAULT_CODEMAP_SOURCE_LIMIT,
) -> FileCodeMap:
    """Verify one selectable text file and emit no invented declarations."""

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
    return FileCodeMap(
        path=project_file.path,
        source_sha256=project_file.sha256,
        source_size_bytes=project_file.size_bytes,
        language=project_file.language,
        analyzer=FALLBACK_ANALYZER,
        parse_status="unsupported",
        line_count=selected.source_line_count,
        diagnostics=(
            ParserDiagnostic(
                code="unsupported_language",
                message=f"no structural extractor is available for {language}",
                severity="info",
            ),
        ),
    )


__all__ = ["FALLBACK_ANALYZER", "extract_fallback_code_map"]
