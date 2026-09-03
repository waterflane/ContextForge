"""Stable version metadata for the ContextForge bridge protocol."""

from typing import Final, Literal

BRIDGE_PROTOCOL_VERSION: Final[Literal["1.1"]] = "1.1"
SUPPORTED_BRIDGE_PROTOCOL_VERSIONS: Final[
    tuple[Literal["1.0", "1.1"], ...]
] = ("1.0", "1.1")

__all__ = ["BRIDGE_PROTOCOL_VERSION", "SUPPORTED_BRIDGE_PROTOCOL_VERSIONS"]
