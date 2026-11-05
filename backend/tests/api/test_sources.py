from types import SimpleNamespace

from fastapi.testclient import TestClient


from app.core_main import app
from app.core.auth import AuthenticatedApplication, get_current_application
from app.core.rate_limit import require_rate_limit
from app.services.rate_limiter import GENERAL_API_LIMIT

test_auth = AuthenticatedApplication(
    application=SimpleNamespace(
        id="test-application",
        slug="test-application",
    ),
    api_key=SimpleNamespace(
        id="test-api-key",
        scopes=[
            "sources:read",
            "sources:write",
        ],
    ),
)

app.dependency_overrides[get_current_application] = (
    lambda: test_auth
)

app.dependency_overrides[
    require_rate_limit(
        bucket="general",
        rate_limit=GENERAL_API_LIMIT,
    )
] = lambda: test_auth

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

def test_list_sources():
    response = client.get("/api/v1/sources")

    assert response.status_code == 200
    data = response.json()

    assert "items" in data
    assert "limit" in data
    assert "offset" in data
    assert "count" in data


def test_list_sources_with_pagination():
    response = client.get("/api/v1/sources?limit=1&offset=0")

    assert response.status_code == 200
    data = response.json()

    assert data["limit"] == 1
    assert data["offset"] == 0
    assert len(data["items"]) <= 1


def test_list_sources_filter_by_publisher():
    response = client.get("/api/v1/sources?publisher=Test Publisher")

    assert response.status_code == 200
    assert isinstance(response.json()["items"], list)


def test_list_sources_filter_by_source_type():
    response = client.get("/api/v1/sources?source_type=news")

    assert response.status_code == 200
    assert isinstance(response.json()["items"], list)


def test_list_sources_filter_by_domain():
    response = client.get("/api/v1/sources?domain=example.com")

    assert response.status_code == 200
    assert isinstance(response.json()["items"], list)


def test_list_sources_search_by_title():
    response = client.get("/api/v1/sources?search=Argus")

    assert response.status_code == 200
    assert isinstance(response.json()["items"], list)


def test_list_sources_rejects_invalid_limit():
    response = client.get("/api/v1/sources?limit=500")

    assert response.status_code == 422