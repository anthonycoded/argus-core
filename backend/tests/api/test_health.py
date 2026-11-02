from fastapi.testclient import TestClient

from app.core_main import app


client = TestClient(app)


def test_core_health():
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "argus-core",
        "version": "v1",
    }


def test_core_health_unknown_route():
    response = client.get("/api/v1/unknown")

    assert response.status_code == 404