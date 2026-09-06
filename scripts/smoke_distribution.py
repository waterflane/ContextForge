"""Provider-independent smoke checks for an installed distribution."""

from __future__ import annotations

from importlib.metadata import distribution
from importlib.util import find_spec

import contextforge
from contextforge._metadata import __version__
from contextforge.bridge import (
    BRIDGE_PROTOCOL_VERSION,
    SUPPORTED_BRIDGE_PROTOCOL_VERSIONS,
    BridgeServer,
)

installed = distribution("contextforge-repo")
assert installed.metadata["Name"] == "contextforge-repo"
assert installed.metadata["Version"] == __version__
assert installed.metadata["License-Expression"] == "Apache-2.0"
assert contextforge.__version__ == __version__
assert BRIDGE_PROTOCOL_VERSION == "1.1"
assert SUPPORTED_BRIDGE_PROTOCOL_VERSIONS == ("1.0", "1.1")
assert BridgeServer.__module__ == "contextforge.bridge.server"
assert find_spec("contextforge.protocol") is None

scripts = {
    entry.name: entry.value
    for entry in installed.entry_points
    if entry.group == "console_scripts"
}
assert scripts["contextforge"] == "contextforge.cli.main:run"
assert scripts["ctxf"] == scripts["contextforge"]

print(f"contextforge-repo {__version__} metadata and entry points are valid")
