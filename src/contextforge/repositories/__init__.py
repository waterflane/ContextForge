"""Repository analysis and inventory domain boundary."""

from contextforge.repositories.analysis import RepositoryAnalyzer
from contextforge.repositories.generated import (
    GeneratedArtifactRegistryError,
    load_generated_artifact_digests,
    register_generated_artifact,
)
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
    "GeneratedArtifactRegistryError",
    "ProjectFile",
    "ProjectSnapshot",
    "RepositoryAnalyzer",
    "ScanOptions",
    "ScanSummary",
    "SkippedFile",
    "load_generated_artifact_digests",
    "register_generated_artifact",
    "scan_repository",
]
