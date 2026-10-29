
import calendar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

from app.config import KEYWORDS


def normalize_url(url: str) -> str:
    """Remove common tracking parameters from article URLs."""
    parts = urlsplit(url.strip())

    query_items = [
        item
        for item in parts.query.split("&")
        if item
        and not item.lower().startswith(
            ("utm_", "fbclid=", "gclid=", "mc_cid=", "mc_eid=")
        )
    ]

    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path.rstrip("/") or "/",
            "&".join(query_items),
            "",
        )
    )


def parse_published(entry) -> datetime | None:
    """Parse RSS publication timestamps into UTC."""
    parsed = entry.get("published_parsed")

    if parsed:
        return datetime.fromtimestamp(
            calendar.timegm(parsed),
            tz=timezone.utc,
        )

    raw = entry.get("published") or entry.get("updated")

    if raw:
        try:
            result = parsedate_to_datetime(raw)

            if result.tzinfo is None:
                result = result.replace(tzinfo=timezone.utc)

            return result.astimezone(timezone.utc)

        except (TypeError, ValueError, OverflowError):
            pass

    return None


def score_topic(
    title: str,
    published_at: datetime | None,
) -> tuple[float, float, float]:
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
