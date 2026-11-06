import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api.v1.router import api_router
from app.core.auth import AuthenticatedApplication, get_current_application
from app.core.rate_limit import require_rate_limit

from app.services.rate_limiter import (
    GENERAL_API_LIMIT,
    ARTICLE_EXTRACTION_LIMIT,
)
from app.factory import create_app


@pytest.fixture
def client():
    test_app = create_app(
        title="Argus-Core Test",
        version="test",
        routers=(api_router,),
    )

    test_auth = AuthenticatedApplication(
        application=SimpleNamespace(
            id="test-application",
            slug="test-application",
        ),
        api_key=SimpleNamespace(
            id="test-api-key",
            scopes=["articles:extract"],
        ),
    )

    test_app.dependency_overrides[get_current_application] = (
        lambda: test_auth
    )

    test_app.dependency_overrides[
        require_rate_limit(
            bucket="general",
            rate_limit=GENERAL_API_LIMIT,
        )
    ] = lambda: test_auth

    test_app.dependency_overrides[
        require_rate_limit(
            bucket="article_extraction",
            rate_limit=ARTICLE_EXTRACTION_LIMIT,
        )
    ] = lambda: test_auth

    with TestClient(test_app) as test_client:
        yield test_client


def test_article_extraction_success(client):
    extraction_result = {
        "url": "https://example.com/article",
        "extraction_status": "success",
        "article_text": "Example extracted article text.",
        "extraction_error": None,
    }

    with patch(
        "app.api.v1.articles.extract_articles",
        new_callable=AsyncMock,
        return_value=[extraction_result],
    ):
        response = client.post(
            "/api/v1/articles/extract",
            json={"url": "https://example.com/article"},
        )

    assert response.status_code == 200
    assert response.json()["extraction_status"] == "success"
    assert response.json()["article_text"] == (
        "Example extracted article text."
    )


def test_article_extraction_invalid_url(client):
    response = client.post(
        "/api/v1/articles/extract",
        json={"url": "not-a-valid-url"},
    )

    assert response.status_code == 422


def test_article_extraction_blocks_ssrf_with_existing_response_contract(client):
    response = client.post(
        "/api/v1/articles/extract",
        json={"url": "http://169.254.169.254/latest/meta-data/"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "url": "http://169.254.169.254/latest/meta-data/",
        "extraction_status": "request_error",
        "article_text": None,
        "extraction_error": "Unsafe article destination.",
    }
