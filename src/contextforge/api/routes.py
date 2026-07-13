"""Minimal local API routes."""

from fastapi import APIRouter

from contextforge._metadata import APP_NAME, __version__
from contextforge.core import HealthStatus, VersionInfo

router = APIRouter()


@router.get("/health", response_model=HealthStatus)
def health() -> HealthStatus:
    """Return local API health status."""

    return HealthStatus(status="ok")


@router.get("/version", response_model=VersionInfo)
def version() -> VersionInfo:
    """Return application version information."""

    return VersionInfo(name=APP_NAME, version=__version__)
