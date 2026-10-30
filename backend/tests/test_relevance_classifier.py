
import pytest

from app.services.ollama_service import classify_article_relevance


TOPIC = (
    "Netanyahu's bloc leading Israel election but short "
    "of majority, partial results show"
)

ARTICLES = [
    {
        "title": "Israeli opposition accuses Netanyahu of trying to delegitimise election",
        "snippet": (
            "The PM's Likud party has alleged foreign interference "
            "after his coalition lost its majority."
        ),
    },
    {
        "title": "Israel's election is deadlocked",
        "snippet": (
            "After 90% of votes in Israel have been counted, "
            "no clear winner has emerged."
        ),
    },
    {
        "title": "How could Israel break election deadlock? Here are four scenarios",
        "snippet": (
            "With more than 90% of votes counted, all signs point "
            "to an impasse. The most likely outcome may be new elections."
        ),
    },
]


@pytest.mark.asyncio
@pytest.mark.parametrize("article", ARTICLES)
async def test_article_relevance_classification(article):
    result = await classify_article_relevance(
        topic=TOPIC,
        title=article["title"],
        snippet=article["snippet"],
    )

    assert result is not None
    assert result.classification in {
        "relevant",
        "partially_relevant",
        "irrelevant",
    }

    print(f"\nArticle: {article['title']}")
    print(f"Classification: {result.classification}")
    print(f"Rationale: {result.rationale}")
