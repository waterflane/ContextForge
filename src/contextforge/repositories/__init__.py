"""Repository analysis and inventory domain boundary."""

from contextforge.repositories.analysis import RepositoryAnalyzer
from contextforge.repositories.models import (
    IgnoredFile,
    ProjectFile,
    ProjectSnapshot,
    ScanOptions,
    ScanSummary,
    SkippedFile,
)
from contextforge.repositories.scanner import scan_repository

__all__ = [
    "IgnoredFile",
    "ProjectFile",
    "ProjectSnapshot",
    "RepositoryAnalyzer",
    "ScanOptions",
    "ScanSummary",
    "SkippedFile",
    "scan_repository",
]
