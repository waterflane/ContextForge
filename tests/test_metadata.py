from importlib.metadata import version

from contextforge import __version__


def test_version() -> None:
    assert version("contextforge-repo") == __version__
