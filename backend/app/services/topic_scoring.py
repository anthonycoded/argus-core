
from datetime import datetime, timezone

from app.config import KEYWORDS
from app.global_chess_config import global_chess_settings


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
        freshness = global_chess_settings.missing_date_freshness
    else:
        age_hours = max(
            0.0,
            (
                datetime.now(timezone.utc) - published_at
            ).total_seconds() / 3600,
        )
        freshness = max(
            0.0,
            100.0 - (age_hours / global_chess_settings.freshness_window_hours * 100.0),
        )

    score = round(relevance * global_chess_settings.relevance_weight
                  + freshness * global_chess_settings.freshness_weight, 2)

    return round(relevance, 2), round(freshness, 2), score
