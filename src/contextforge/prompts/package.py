"""Minimal prompt package data structure."""

from pydantic import BaseModel, ConfigDict


class PromptPackage(BaseModel):
    """A future model- or agent-targeted prompt package."""

    title: str
    body: str

    model_config = ConfigDict(frozen=True)
