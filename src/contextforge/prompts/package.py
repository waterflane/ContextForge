"""Minimal prompt package data structure."""

from pydantic import BaseModel, ConfigDict


class PromptPackage(BaseModel):
    """Portable compiled prompt text without any execution behavior."""

    title: str
    body: str

    model_config = ConfigDict(frozen=True, extra="forbid")
