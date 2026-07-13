"""Minimal context package data structure."""

from pydantic import BaseModel, ConfigDict, Field


class ContextPackage(BaseModel):
    """A future reviewable context package."""

    title: str
    items: tuple[str, ...] = Field(default_factory=tuple)

    model_config = ConfigDict(frozen=True)
