from fastapi.testclient import TestClient

from app.core_main import app

client = TestClient(app)


def test_normalize_source_removes_tracking_parameters():
    response = client.post(
        "/api/v1/sources/normalize",
        json={
            "title": "Example article",
            "url": "https://example.com/article?utm_source=rss&fbclid=123",
            "publisher": "Example News",
            "published_at": "2026-11-01T12:00:00Z",
            "source_type": "rss",
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["url"] == "https://example.com/article"
    assert data["domain"] == "example.com"
    assert data["publisher"] == "Example News"
    assert data["source_type"] == "rss"


def test_normalize_source_converts_timestamp_to_utc():
    response = client.post(
        "/api/v1/sources/normalize",
        json={
            "title": "Example article",
            "url": "https://example.com/article",
            "published_at": "2026-11-01T07:00:00-05:00",
        },
    )

    assert response.status_code == 200
    assert response.json()["published_at"] == "2026-11-01T12:00:00Z"


def test_normalize_source_rejects_invalid_timestamp():
    response = client.post(
        "/api/v1/sources/normalize",
        json={
            "title": "Example article",
            "url": "https://example.com/article",
            "published_at": "not-a-date",
        },
    )

    assert response.status_code == 422


def test_normalize_source_rejects_invalid_url():
    response = client.post(
        "/api/v1/sources/normalize",
        json={
            "title": "Example article",
            "url": "not-a-valid-url",
        },
    )

    assert response.status_code == 422