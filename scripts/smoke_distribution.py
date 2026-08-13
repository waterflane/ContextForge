"""Provider-independent smoke checks for an installed distribution."""

from __future__ import annotations

from importlib.metadata import distribution

import contextforge
from contextforge._metadata import __version__

installed = distribution("contextforge-cli")
assert installed.metadata["Name"] == "contextforge-cli"
assert installed.metadata["Version"] == __version__
assert installed.metadata["License-Expression"] == "Apache-2.0"
assert contextforge.__version__ == __version__

scripts = {
    entry.name: entry.value
    for entry in installed.entry_points
    if entry.group == "console_scripts"
}
assert scripts["contextforge"] == "contextforge.cli.main:run"
assert scripts["ctxf"] == scripts["contextforge"]

print(f"contextforge-cli {__version__} metadata and entry points are valid")
