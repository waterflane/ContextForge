"""Small provider-independent validators shared by closed domain contracts."""

from __future__ import annotations

import re
from typing import Annotated

from pydantic import Field

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def validate_portable_relative_path(value: str) -> str:
    """Validate one already-canonical portable path without filesystem access."""

    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("path must be a canonical portable relative path")
    if value.startswith("/") or _WINDOWS_DRIVE.match(value):
        raise ValueError("path must be a canonical portable relative path")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("path must be a canonical portable relative path")
    return value


__all__ = ["Sha256", "validate_portable_relative_path"]
