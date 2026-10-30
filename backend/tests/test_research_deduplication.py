
from unittest.mock import AsyncMock, patch

import pytest

from app.services.research_service import discover_sources


@pytest.mark.asyncio
async def test_processed_urls_are_not_classified_again():
    processed_urls = {
        "https://example.com/article"
    }

    candidates = [
        {
            "title": "Netanyahu election results",
            "url": "https://example.com/article",
            "publisher": "Example",
            "published_at": None,
            "snippet": "Election results",
            "source_type": "rss",
            "_relevance": 1.0,
        }
    ]

    with (
        patch(
            "app.services.research_service.discover_rss_sources",
            return_value=candidates,
        ),
        patch(
            "app.services.research_service.discover_google_news_sources",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.services.research_service.classify_article_relevance",
            new_callable=AsyncMock,
        ) as classifier,
    ):
        results = await discover_sources(
            query="Netanyahu Israel election",
            processed_urls=processed_urls,
        )

    assert results == []
    classifier.assert_not_called()


@pytest.mark.asyncio
async def test_partial_matches_are_not_duplicated():
    url = "https://example.com/article"

    candidates = [
        {
            "title": "Netanyahu election results",
            "url": url,
            "publisher": "Example",
            "published_at": None,
            "snippet": "Election results",
            "source_type": "rss",
            "_relevance": 1.0,
        }
    ]

    review_queue = []

    partial_result = type(
        "Classification",
        (),
        {
            "classification": "partially_relevant",
            "rationale": "Related to the election",
        },
    )()

    with (
        patch(
            "app.services.research_service.discover_rss_sources",
            return_value=candidates,
        ),
        patch(
            "app.services.research_service.discover_google_news_sources",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "app.services.research_service.classify_article_relevance",
            new_callable=AsyncMock,
            return_value=partial_result,
        ) as classifier,
    ):
        await discover_sources(
            query="Netanyahu Israel election",
            review_queue=review_queue,
        )

        await discover_sources(
            query="Netanyahu Israel election",
            review_queue=review_queue,
        )

    assert len(review_queue) == 1
    assert review_queue[0]["url"] == url

    # The article should only have been classified once.
    assert classifier.call_count == 1
