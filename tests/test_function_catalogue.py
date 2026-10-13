"""Source-bound function descriptions and deterministic complementary discovery."""

import asyncio
from pathlib import Path

import pytest

from contextforge.application import build_repository_index


def test_function_catalogue_describes_and_searches_verified_dependencies(
    tmp_path: Path,
) -> None:
    from contextforge.intelligence import load_manifest
    from contextforge.intelligence.function_catalogue import (
        FunctionCatalogue,
        describe_function,
    )

    source = tmp_path / "source"
    source.mkdir()
    (source / "values.py").write_text(
        'SECRET_KEYS = {"password", "secret"}\n'
        "def cleanse(payload: dict) -> dict:\n"
        '    """Recursively redact structured fields."""\n'
        "    if not payload:\n"
        '        raise ValueError("empty")\n'
        "    return {key: None if key in SECRET_KEYS else value "
        "for key, value in payload.items()}\n"
        "def handle(fields):\n"
        "    return cleanse(fields)\n"
        "def top(fields):\n"
        "    return handle(fields)\n"
    )
    asyncio.run(
        build_repository_index(
            source, provider=None, provider_configuration=None, semantic_scope="none"
        )
    )
    catalogue = FunctionCatalogue(source, load_manifest(source))
    matches = catalogue.search("recursive secret redaction structured")
    assert matches and matches[0].symbol.name == "cleanse"
    description = describe_function(matches[0].symbol)
    assert "payload: dict" in description
    assert "Declared return type: dict" in description
    assert "ValueError" in description
    assert "payload.items" in description
    assert matches[0].callers == ("values.handle", "values.top")
    handle = catalogue.search("values.handle")[0]
    assert handle.callees == ("values.cleanse",)
    (source / "values.py").write_text("def different():\n    return None\n")
    with pytest.raises(ValueError, match="source changed"):
        FunctionCatalogue(source, load_manifest(source))


def test_function_catalogue_searches_module_context_and_inflections(
    tmp_path: Path,
) -> None:
    from contextforge.intelligence import load_manifest
    from contextforge.intelligence.function_catalogue import FunctionCatalogue

    source = tmp_path / "source"
    source.mkdir()
    (source / "records.py").write_text(
        '"""Event logging for delivery diagnostics."""\n'
        "def tidy(message):\n"
        "    return message.strip()\n"
        "def emit(record):\n"
        "    return tidy(record)\n"
    )
    asyncio.run(
        build_repository_index(
            source, provider=None, provider_configuration=None, semantic_scope="none"
        )
    )
    catalogue = FunctionCatalogue(source, load_manifest(source))
    matches = catalogue.search("event logs messages")
    assert matches[0].symbol.name == "tidy"
    assert "Event logging" in str(matches[0].card()["module_description"])
    assert catalogue.search("emit")[0].card()["verified_callees"] == ("records.tidy",)


def test_function_catalogue_retains_relevant_call_endpoint_before_truncation(
    tmp_path: Path,
) -> None:
    from contextforge.intelligence import load_manifest
    from contextforge.intelligence.function_catalogue import FunctionCatalogue

    source = tmp_path / "source"
    source.mkdir()
    phrase = "selected file changes " * 6
    (source / "operations.py").write_text(
        f'def checker(value):\n    """{phrase}"""\n    return value\n'
        'def entry(value):\n    """Read selected file."""\n    return checker(value)\n'
    )
    for number in range(12):
        (source / f"other_{number}.py").write_text(
            f'def unrelated_{number}(value):\n    """{phrase}"""\n    return value\n'
        )
    asyncio.run(
        build_repository_index(
            source, provider=None, provider_configuration=None, semantic_scope="none"
        )
    )
    catalogue = FunctionCatalogue(source, load_manifest(source))
    matches = catalogue.search("selected file changes")
    assert len(matches) == 12
    assert "entry" in {entry.symbol.name for entry in matches}
    assert matches == catalogue.search("selected file changes")
    saved_links = catalogue.links
    catalogue.links = {key: set() for key in saved_links}
    assert "entry" not in {
        entry.symbol.name for entry in catalogue.search("selected file changes")
    }
