
import pytest
import httpx

from unittest.mock import AsyncMock, MagicMock, patch

from app.services.article_extraction_service import (
    extract_article,
    extract_articles,
)


@pytest.fixture
def sample_url():
    return "https://example.com/article"


@pytest.fixture
def sample_article_text():
    return (
        "This is a sufficiently long article body. "
        * 30
    )


@pytest.fixture
def sample_response():
    response = MagicMock()
    response.status_code = 200
    response.headers = {
        "content-type": "text/html; charset=utf-8"
    }
    response.text = "<html><body>Article content</body></html>"
    response.raise_for_status = MagicMock()
    return response


@pytest.mark.asyncio
async def test_extract_article_success(
    sample_url,
    sample_article_text,
    sample_response,
):
    client = AsyncMock()
    client.get.return_value = sample_response

    with patch(
        "app.services.article_extraction_service.trafilatura.extract",
        return_value=sample_article_text,
    ):
        result = await extract_article(sample_url, client)

    assert result["extraction_status"] == "success"
    assert result["article_text"] == sample_article_text.strip()
    assert result["extraction_error"] is None


@pytest.mark.asyncio
async def test_extract_article_rejects_invalid_url():
    client = AsyncMock()

    result = await extract_article(
        "invalid-url",
        client,
    )

    assert result["extraction_status"] == "failed"
    assert result["extraction_error"] == "Invalid URL"
    client.get.assert_not_called()


@pytest.mark.asyncio
async def test_extract_article_handles_http_error(sample_url):
    client = AsyncMock()

    request = httpx.Request("GET", sample_url)
    response = httpx.Response(
        403,
        request=request,
    )

    client.get.side_effect = httpx.HTTPStatusError(
        "Forbidden",
        request=request,
        response=response,
    )

    result = await extract_article(sample_url, client)

    assert result["extraction_status"] == "http_error"
    assert result["extraction_error"] == "HTTP 403"


@pytest.mark.asyncio
async def test_extract_article_handles_timeout(sample_url):
    client = AsyncMock()
    client.get.side_effect = httpx.ReadTimeout("Request timed out")

    result = await extract_article(sample_url, client)

    assert result["extraction_status"] == "timeout"
    assert result["extraction_error"] == "Request timed out."


@pytest.mark.asyncio
async def test_extract_article_rejects_unsupported_content_type(
    sample_url,
):
    client = AsyncMock()

    response = MagicMock()
    response.headers = {
        "content-type": "application/pdf"
    }
    response.raise_for_status = MagicMock()

    client.get.return_value = response

    result = await extract_article(sample_url, client)

    assert result["extraction_status"] == "unsupported_content"
    assert "application/pdf" in result["extraction_error"]


@pytest.mark.asyncio
async def test_extract_article_handles_missing_content(
    sample_url,
    sample_response,
):
    client = AsyncMock()
    client.get.return_value = sample_response

    with patch(
        "app.services.article_extraction_service.trafilatura.extract",
        return_value=None,
    ):
        result = await extract_article(sample_url, client)

    assert result["extraction_status"] == "no_content"
    assert result["article_text"] is None


@pytest.mark.asyncio
async def test_extract_articles_preserves_source_metadata(
    sample_url,
):
    sources = [
        {
            "title": "Example geopolitical article",
            "url": sample_url,
            "publisher": "Example News",
            "snippet": "Original article snippet",
            "source_type": "rss",
        }
    ]

    extraction_result = {
        "extraction_status": "success",
        "article_text": "Extracted article body",
        "extraction_error": None,
    }

    with patch(
        "app.services.article_extraction_service.httpx.AsyncClient"
    ) as client_class:
        mock_client = AsyncMock()
        client_class.return_value.__aenter__.return_value = mock_client

        with patch(
            "app.services.article_extraction_service.extract_article",
            new_callable=AsyncMock,
            return_value=extraction_result,
        ):
            results = await extract_articles(sources)

    assert len(results) == 1
    assert results[0]["title"] == "Example geopolitical article"
    assert results[0]["publisher"] == "Example News"
    assert results[0]["snippet"] == "Original article snippet"
    assert results[0]["article_text"] == "Extracted article body"
    assert results[0]["extraction_status"] == "success"
