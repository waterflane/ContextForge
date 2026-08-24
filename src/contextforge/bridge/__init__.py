"""Persistent generic ContextForge bridge protocol v1."""

from .server import (
    BRIDGE_PROTOCOL_VERSION,
    JSONRPC_VERSION,
    MAX_JSONRPC_MESSAGE_BYTES,
    BridgeFault,
    BridgeServer,
    ContextForgeBridge,
    serve_stdio_bridge,
)

__all__ = [
    "BRIDGE_PROTOCOL_VERSION",
    "JSONRPC_VERSION",
    "MAX_JSONRPC_MESSAGE_BYTES",
    "BridgeFault",
    "BridgeServer",
    "ContextForgeBridge",
    "serve_stdio_bridge",
]
