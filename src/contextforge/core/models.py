"""Small shared data contracts for the project foundation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class HealthStatus(BaseModel):
    """Health response for local interfaces."""

    status: Literal["ok"]

    model_config = ConfigDict(frozen=True)


class VersionInfo(BaseModel):
    """Application version response."""

    name: str
    version: str

    model_config = ConfigDict(frozen=True)
