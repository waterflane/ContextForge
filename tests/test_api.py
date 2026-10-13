import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from contextforge import __version__
from contextforge.api.app import create_app
from contextforge.application import build_repository_index


def test_health_endpoint() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_endpoint() -> None:
    client = TestClient(create_app())

    response = client.get("/version")

    assert response.status_code == 200
    assert response.json() == {"name": "ContextForge", "version": __version__}


def test_read_only_index_v3_endpoints(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "def run(value: int) -> int:\n    return value + 1\n", encoding="utf-8"
    )
    (tmp_path / ".contextforge").mkdir()
    (tmp_path / ".contextforge" / "config.toml").write_text(
        'config_version = 1\n[models]\nprovider = "fake"\nmodel = "fixture"\n',
        encoding="utf-8",
    )
    asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    client = TestClient(create_app())
    root = str(tmp_path)

    mapped = client.post("/v1/map", json={"repository_root": root})
    mapped_with_graph = client.post(
        "/v1/map", json={"repository_root": root, "include_graph": True}
    )
    searched = client.post(
        "/v1/search",
        json={"repository_root": root, "task": "run", "planning_mode": "off"},
    )
    symbols = client.post("/v1/symbol", json={"repository_root": root, "query": "run"})
    compiled = client.post(
        "/v1/compile",
        json={
            "repository_root": root,
            "task": "change run",
            "working_files": ["app.py"],
            "context_window_tokens": 8_000,
            "response_tokens": 200,
            "safety_margin_tokens": 100,
            "planning_mode": "off",
        },
    )

    assert mapped.status_code == searched.status_code == 200
    assert symbols.status_code == compiled.status_code == 200
    assert mapped.json()["orientation"]["files"][0]["path"] == "app.py"
    assert set(mapped.json()["repository_maps"]) == {
        "architecture",
        "conventions",
        "features",
    }
    assert mapped_with_graph.json()["relationship_graph"]["schema_version"] == 3
    assert searched.json()["provider_calls"] == 0
    assert not searched.json()["evidence_diagnostics"]["compiler_materialized"]
    assert symbols.json()["symbols"][0]["name"] == "run"
    assert compiled.json()["capsule"]["schema_version"] == 2
    assert (
        compiled.json()["evidence_diagnostics"]
        == compiled.json()["capsule"]["evidence_diagnostics"]
    )
    assert (
        compiled.json()["evidence_diagnostics"]["effective_sufficiency"]
        == (compiled.json()["compilation_sufficiency"]["effective_status"])
    )

    auto_searched = client.post(
        "/v1/search",
        json={"repository_root": root, "task": "run", "planning_mode": "auto"},
    )
    required_search = client.post(
        "/v1/search",
        json={
            "repository_root": root,
            "task": "run",
            "planning_mode": "required",
        },
    )
    ranged = client.post(
        "/v1/compile",
        json={
            "repository_root": root,
            "task": "change run",
            "working_lines": [{"path": "app.py", "start_line": 1, "end_line": 2}],
            "context_window_tokens": 8_000,
            "response_tokens": 200,
            "safety_margin_tokens": 100,
            "planning_mode": "auto",
        },
    )
    partial_symbol = client.post(
        "/v1/symbol",
        json={"repository_root": root, "query": "app.run"},
    )
    assert auto_searched.status_code == ranged.status_code == 200
    assert auto_searched.json()["diagnostics"] == ["planner_provider_failure"]
    assert required_search.status_code == 400
    assert "provider" in required_search.json()["detail"]
    assert ranged.json()["capsule"]["working_set"][0]["path"] == "app.py"
    assert partial_symbol.status_code == 200

    missing = client.post(
        "/v1/map", json={"repository_root": str(tmp_path / "missing")}
    )
    too_small = client.post(
        "/v1/compile",
        json={
            "repository_root": root,
            "task": "run",
            "context_window_tokens": 1,
            "response_tokens": 0,
            "safety_margin_tokens": 0,
            "planning_mode": "off",
        },
    )
    assert missing.status_code == too_small.status_code == 400
    assert "detail" in missing.json() and "detail" in too_small.json()

    invalid_range = client.post(
        "/v1/compile",
        json={
            "repository_root": root,
            "task": "run",
            "working_lines": [{"path": "app.py", "start_line": 3, "end_line": 1}],
        },
    )
    assert invalid_range.status_code == 422

    unindexed = tmp_path / "unindexed"
    unindexed.mkdir()
    (unindexed / "plain.txt").write_text("plain\n", encoding="utf-8")
    for endpoint, payload in (
        ("search", {"task": "plain"}),
        ("symbol", {"query": "plain"}),
        ("compile", {"task": "plain"}),
    ):
        response = client.post(
            f"/v1/{endpoint}",
            json={"repository_root": str(unindexed), **payload},
        )
        assert response.status_code == 400
