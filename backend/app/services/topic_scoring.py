
from datetime import datetime, timezone

from app.config import KEYWORDS


def score_topic(
    title: str,
    published_at: datetime | None,
) -> tuple[float, float, float]:
    """Score a topic using Global Chess relevance and freshness rules."""
    text = title.casefold()

    raw_relevance = sum(
        weight
        for phrase, weight in KEYWORDS.items()
        if phrase in text
    )
    relevance = min(float(raw_relevance), 100.0)

    if published_at is None:
        freshness = 40.0
    else:
        age_hours = max(
            0.0,
            (
                datetime.now(timezone.utc) - published_at
            ).total_seconds() / 3600,
        )
        freshness = max(
            0.0,
            100.0 - (age_hours / 72.0 * 100.0),
        )

    score = round(relevance * 0.7 + freshness * 0.3, 2)

    return round(relevance, 2), round(freshness, 2), score
