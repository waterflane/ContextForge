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
    asyncio.run(
        build_repository_index(tmp_path, provider=None, provider_configuration=None)
    )
    client = TestClient(create_app())
    root = str(tmp_path)

    mapped = client.post("/v1/map", json={"repository_root": root})
    mapped_with_graph = client.post(
        "/v1/map", json={"repository_root": root, "include_graph": True}
    )
    searched = client.post("/v1/search", json={"repository_root": root, "task": "run"})
    symbols = client.post("/v1/symbol", json={"repository_root": root, "query": "run"})
    compiled = client.post(
        "/v1/compile",
        json={
            "repository_root": root,
            "task": "change run",
            "working_files": ["app.py"],
            "context_window_tokens": 2_000,
            "response_tokens": 200,
            "safety_margin_tokens": 100,
        },
    )

    assert mapped.status_code == searched.status_code == 200
    assert symbols.status_code == compiled.status_code == 200
    assert mapped.json()["orientation"]["files"][0]["path"] == "app.py"
    assert mapped_with_graph.json()["relationship_graph"]["schema_version"] == 3
    assert searched.json()["provider_calls"] == 0
    assert symbols.json()["symbols"][0]["name"] == "run"
    assert compiled.json()["capsule"]["schema_version"] == 2

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
