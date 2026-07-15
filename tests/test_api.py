from fastapi.testclient import TestClient

from contextforge.api.app import create_app


def test_health_endpoint() -> None:
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_version_endpoint() -> None:
    client = TestClient(create_app())

    response = client.get("/version")

    assert response.status_code == 200
    assert response.json() == {"name": "ContextForge", "version": "0.3.0"}
