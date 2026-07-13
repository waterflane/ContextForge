"""Core domain package.

This package must remain independent from user interfaces, storage adapters,
model providers, and editor integrations.
"""

from contextforge.core.models import HealthStatus, VersionInfo

__all__ = ["HealthStatus", "VersionInfo"]
