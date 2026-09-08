import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

import contextforge.application as application_module
from contextforge.application import build_repository_index
from contextforge.intelligence import (
    load_manifest,
    load_orientation_map,
    load_relationship_graph,
)
from contextforge.models import ModelProvider


def test_structural_generation_contains_deterministic_graph_and_orientation(
    tmp_path: Path,
) -> None:
    (tmp_path / "main.py").write_text(
        "from service import handle\n\nhandle()\n", encoding="utf-8"
    )
    (tmp_path / "service.py").write_text(
        "def handle() -> str:\n    return 'ok'\n", encoding="utf-8"
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_service.py").write_text(
        "from service import handle\n\n"
        "def test_handle():\n"
        "    assert handle() == 'ok'\n",
        encoding="utf-8",
    )

    report = asyncio.run(
        build_repository_index(
            tmp_path,
            provider=None,
            provider_configuration=None,
        )
    )
    graph = load_relationship_graph(tmp_path, manifest=report.manifest)
    orientation = load_orientation_map(tmp_path, manifest=report.manifest)

    assert report.manifest.schema_version == 3
    assert report.manifest.generation_kind == "enriched"
    assert (
        report.manifest.build.previous_generation_id
        == report.structural.manifest.generation_id
    )
    assert {item.path for item in graph.file_metrics} == {
        "main.py",
        "service.py",
        "tests/test_service.py",
    }
    assert any(
        edge.kind == "import" and edge.provenance == "verified" for edge in graph.edges
    )
    assert any(
        edge.kind == "entrypoint-handler"
        and edge.provenance == "best-effort-structural"
        for edge in graph.edges
    )
    assert any(edge.kind == "source-test" for edge in graph.edges)
    assert tuple(item.path for item in orientation.files) == (
        "main.py",
        "service.py",
        "tests/test_service.py",
    )
    assert load_relationship_graph(tmp_path, manifest=report.manifest) == graph


def test_enrichment_failure_keeps_published_structural_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")

    async def fail_enrichment(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        raise RuntimeError("enrichment failed")

    monkeypatch.setattr(
        application_module, "build_semantic_card_index", fail_enrichment
    )
    with pytest.raises(RuntimeError, match="enrichment failed"):
        asyncio.run(
            build_repository_index(
                tmp_path,
                provider=cast(ModelProvider, object()),
                provider_configuration=None,
            )
        )

    active = load_manifest(tmp_path)
    assert active.generation_kind == "structural"
    assert active.artifacts.relationship_graph is not None
    assert (
        load_relationship_graph(tmp_path, manifest=active).file_metrics[0].path
        == "app.py"
    )
