
import asyncio
import calendar
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import feedparser
import httpx

from app.config import FEEDS
from app.services.rss_service import normalize_url, parse_published

logger = logging.getLogger(__name__)

GDELT_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
MIN_GDELT_INTERVAL = 5.5
GDELT_COOLDOWN_SECONDS = 300

_last_gdelt_request = 0.0
_gdelt_cooldown_until = 0.0
_gdelt_lock = asyncio.Lock()


def _parse_gdelt_date(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ")
        return parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _feed_matches_topic(title: str, query: str) -> bool:
    """Basic local relevance check without another external API call."""
    title_words = {
        word.casefold()
        for word in title.split()
        if len(word) > 2
    }
    query_words = {
        word.casefold()
        for word in query.split()
        if len(word) > 2
    }

    if not title_words or not query_words:
        return False

    # Require some shared words, but don't require the entire headline
    # to appear in the feed article title.
    overlap = title_words & query_words
    return len(overlap) >= min(2, len(query_words))


def discover_rss_sources(query: str, limit: int = 10) -> list[dict]:
    """Discover relevant articles from the application's configured RSS feeds."""
    results = []
    seen_urls = set()

    for source, feed_url in FEEDS.items():
        try:
            feed = feedparser.parse(feed_url)

            if getattr(feed, "bozo", False) and not feed.entries:
                logger.warning("Could not parse RSS feed: %s", source)
                continue

            for entry in feed.entries:
                title = (entry.get("title") or "").strip()
                link = (entry.get("link") or "").strip()

                if not title or not link:
                    continue

                if not link.startswith(("https://", "http://")):
                    continue

                if not _feed_matches_topic(title, query):
                    continue

                url = normalize_url(link)

                if url in seen_urls:
                    continue

                seen_urls.add(url)

                results.append({
                    "title": title[:1000],
                    "url": url[:2048],
                    "publisher": source[:255],
                    "published_at": parse_published(entry),
                    "snippet": (
                        entry.get("summary")
                        or entry.get("description")
                        or None
                    ),
                    "source_type": "rss",
                })

                if len(results) >= limit:
                    return results

        except Exception:
            logger.exception("RSS research failed for feed: %s", source)

    return results


async def _wait_for_gdelt_slot() -> bool:
    """Wait between calls and respect this process's GDELT cooldown."""
    global _last_gdelt_request

    async with _gdelt_lock:
        now = time.monotonic()

        if now < _gdelt_cooldown_until:
            return False

        wait_time = MIN_GDELT_INTERVAL - (now - _last_gdelt_request)

        if wait_time > 0:
            await asyncio.sleep(wait_time)

        _last_gdelt_request = time.monotonic()
        return True


async def discover_gdelt_sources(
    query: str,
    limit: int = 10,
) -> list[dict]:
    """Discover articles through GDELT, respecting a local cooldown."""
    global _gdelt_cooldown_until

    allowed = await _wait_for_gdelt_slot()

    if not allowed:
        logger.info("Skipping GDELT during cooldown")
        return []

    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": limit,
        "sort": "HybridRel",
    }

    async with httpx.AsyncClient(
        timeout=30.0,
        headers={"User-Agent": "GlobalChessResearch/1.0"},
    ) as client:
        response = await client.get(GDELT_API_URL, params=params)

    if response.status_code == 429:
        _gdelt_cooldown_until = (
            time.monotonic() + GDELT_COOLDOWN_SECONDS
        )
        logger.warning("GDELT rate-limited; cooling down for 5 minutes")
        return []

    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError:
        logger.warning("GDELT returned a non-JSON response")
        return []

    results = []

    for article in payload.get("articles", []):
        title = (article.get("title") or "").strip()
        link = (article.get("url") or "").strip()

        if not title or not link:
            continue

        if not link.startswith(("https://", "http://")):
            continue

        results.append({
            "title": title[:1000],
            "url": normalize_url(link)[:2048],
            "publisher": (article.get("domain") or "")[:255] or None,
            "published_at": _parse_gdelt_date(article.get("seendate")),
            "snippet": None,
            "source_type": "news",
        })

    return results


async def discover_sources(query: str, limit: int = 10) -> list[dict]:
    """Prefer RSS; consult GDELT only when RSS returns too few sources."""
    rss_results = await asyncio.to_thread(
        discover_rss_sources,
        query,
        limit,
    )

    if len(rss_results) >= limit:
        return rss_results

    try:
        gdelt_results = await discover_gdelt_sources(
            query=query,
            limit=limit - len(rss_results),
        )
    except httpx.HTTPError:
        logger.exception("GDELT source discovery failed")
        gdelt_results = []

    seen_urls = {item["url"] for item in rss_results}

    for item in gdelt_results:
        url = normalize_url(item["url"])

        if url in seen_urls:
            continue

        seen_urls.add(url)
        item["url"] = url
        rss_results.append(item)

        if len(rss_results) >= limit:
            break

    return rss_results
