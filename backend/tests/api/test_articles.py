from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.core_main import app

client = TestClient(app)


def test_article_extraction_success():
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


def test_article_extraction_invalid_url():
    response = client.post(
        "/api/v1/articles/extract",
        json={"url": "not-a-valid-url"},
    )

    assert response.status_code == 422