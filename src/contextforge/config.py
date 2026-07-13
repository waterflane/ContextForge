"""Configuration foundation for ContextForge."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Runtime settings shared by ContextForge entry points."""

    app_name: str = "ContextForge"
    environment: str = "development"
    log_level: LogLevel = Field(default="INFO")

    model_config = SettingsConfigDict(
        env_prefix="CONTEXTFORGE_",
        env_file=".env",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""

    return Settings()
