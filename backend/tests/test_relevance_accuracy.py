
import pytest

from app.services.ollama_service import classify_article_relevance


ARTICLES = [
    # DIRECTLY RELEVANT
    {
        "topic": "Netanyahu's bloc leading Israel election but short of majority",
        "title": "Netanyahu bloc leads Israeli election but falls short of majority",
        "snippet": "Partial election results show Netanyahu's coalition lacks enough seats to form a government.",
        "expected": "relevant",
    },
    {
        "topic": "Netanyahu's bloc leading Israel election but short of majority",
        "title": "Israeli parties begin coalition negotiations after inconclusive election",
        "snippet": "Party leaders discuss possible alliances following the election results.",
        "expected": "relevant",
    },
    {
        "topic": "Netanyahu's bloc leading Israel election but short of majority",
        "title": "Israel opposition leader rejects coalition deal with Netanyahu",
        "snippet": "Opposition leaders respond to the election results and rule out joining Netanyahu's government.",
        "expected": "partially_relevant",
    },

    # SUPPORTING CONTEXT
    {
        "topic": "Netanyahu's bloc leading Israel election but short of majority",
        "title": "How Israel's proportional electoral system works",
        "snippet": "An explanation of parliamentary seats, electoral thresholds, and coalition governments in Israel.",
        "expected": "partially_relevant",
    },
    {
        "topic": "Netanyahu's bloc leading Israel election but short of majority",
        "title": "Who are the major political parties in Israel?",
        "snippet": "A guide to Likud, opposition parties, and smaller political factions that influence coalition formation.",
        "expected": "partially_relevant",
    },
    {
        "topic": "Netanyahu's bloc leading Israel election but short of majority",
        "title": "Israel's political system and history of coalition governments",
        "snippet": "An overview of how Israeli governments are formed and why elections frequently produce political deadlock.",
        "expected": "partially_relevant",
    },

    # PARTIALLY RELEVANT
    {
        "topic": "Netanyahu's bloc leading Israel election but short of majority",
        "title": "Israel's economy faces uncertainty amid political instability",
        "snippet": "The article primarily examines inflation and economic growth, briefly mentioning the recent election.",
        "expected": "irrelevant",
    },
    {
        "topic": "Netanyahu's bloc leading Israel election but short of majority",
        "title": "Protests continue across Israel over judicial reforms",
        "snippet": "The article focuses on demonstrations and judicial reform, with a brief reference to Netanyahu's political position.",
        "expected": "irrelevant",
    },
    {
        "topic": "Netanyahu's bloc leading Israel election but short of majority",
        "title": "Middle East tensions increase following regional military developments",
        "snippet": "The article focuses on regional security developments and briefly mentions Israel's domestic political situation.",
        "expected": "irrelevant",
    },

    # IRRELEVANT
    {
        "topic": "Netanyahu's bloc leading Israel election but short of majority",
        "title": "Japan announces new economic stimulus package",
        "snippet": "The Japanese government announces spending measures to support domestic businesses.",
        "expected": "irrelevant",
    },
    {
        "topic": "Netanyahu's bloc leading Israel election but short of majority",
        "title": "US technology companies report record quarterly earnings",
        "snippet": "Major technology companies report revenue growth and increased profits.",
        "expected": "irrelevant",
    },
    {
        "topic": "Netanyahu's bloc leading Israel election but short of majority",
        "title": "Brazil announces new environmental protection regulations",
        "snippet": "The Brazilian government introduces new regulations covering deforestation and environmental enforcement.",
        "expected": "irrelevant",
    },
]


@pytest.mark.asyncio
@pytest.mark.parametrize("article", ARTICLES)
async def test_relevance_accuracy(article):
    result = await classify_article_relevance(
        topic=article["topic"],
        title=article["title"],
        snippet=article["snippet"],
    )

    assert result is not None, (
        f"Classification failed: {article['title']}"
    )

    assert result.classification == article["expected"], (
        f"\nArticle: {article['title']}"
        f"\nExpected: {article['expected']}"
        f"\nActual: {result.classification}"
        f"\nRationale: {result.rationale}"
    )
