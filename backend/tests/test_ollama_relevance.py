
import pytest

from app.services.ollama_service import classify_article_relevance


@pytest.mark.asyncio
async def test_relevant_israel_election_article():
    result = await classify_article_relevance(
        topic=(
            "Israel's election deadlock is a sign "
            "of the country's deep divisions"
        ),
        title=(
            "How could Israel break election deadlock? "
            "Here are four scenarios"
        ),
        snippet="Analysis of possible outcomes of Israel's election.",
    )

    assert result is not None
    assert result.classification == "relevant"


@pytest.mark.asyncio
async def test_unrelated_us_election_article():
    result = await classify_article_relevance(
        topic=(
            "Israel's election deadlock is a sign "
            "of the country's deep divisions"
        ),
        title=(
            "These are the House races to watch "
            "in the 2026 midterm election"
        ),
        snippet="A look at competitive United States congressional races.",
    )

    assert result is not None
    assert result.classification == "irrelevant"
