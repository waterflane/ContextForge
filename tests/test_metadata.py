from importlib.metadata import version

from contextforge import __version__


def test_version() -> None:
    assert version("contextforge-cli") == __version__
