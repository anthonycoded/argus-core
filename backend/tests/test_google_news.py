import pytest

from app.services.research_service import (
    discover_google_news_sources,
)


@pytest.mark.asyncio
async def test_google_news_discovers_articles():
    query = "Netanyahu Israel election coalition majority"

    results = await discover_google_news_sources(
        query=query,
        limit=10,
    )

    assert isinstance(results, list)

    for article in results:
        assert article["title"]
        assert article["url"].startswith(("https://", "http://"))
        assert article["source_type"] == "google_news"


@pytest.mark.asyncio
async def test_google_news_handles_empty_query():
    results = await discover_google_news_sources(
        query="",
        limit=10,
    )

    assert isinstance(results, list)