"""Core domain package.

This package must remain independent from user interfaces, storage adapters,
model providers, and editor integrations.
"""

from contextforge.core.models import HealthStatus, VersionInfo
from contextforge.progress import (
    NO_OP_PROGRESS_OBSERVER,
    PROGRESS_SCHEMA_VERSION,
    NoOpProgressObserver,
    ProgressEvent,
    ProgressObserver,
    ProgressReporter,
    ProgressStatus,
)

__all__ = [
    "NO_OP_PROGRESS_OBSERVER",
    "PROGRESS_SCHEMA_VERSION",
    "HealthStatus",
    "NoOpProgressObserver",
    "ProgressEvent",
    "ProgressObserver",
    "ProgressReporter",
    "ProgressStatus",
    "VersionInfo",
]
