
import asyncio
import calendar
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import feedparser
import httpx
import re

from app.config import FEEDS
from app.services.rss_service import normalize_url, parse_published
from app.services.ollama_service import classify_article_relevance

logger = logging.getLogger(__name__)

GDELT_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
MIN_GDELT_INTERVAL = 5.5
GDELT_COOLDOWN_SECONDS = 300

_last_gdelt_request = 0.0
_gdelt_cooldown_until = 0.0
_gdelt_lock = asyncio.Lock()

GENERIC_TERMS = {
    "the", "and", "for", "from", "with", "that", "this", "into",
    "after", "before", "over", "under", "about", "their", "they",
    "country", "countries", "government", "official", "officials",
    "report", "reports", "says", "said", "news", "latest",
    "election", "elections", "politics", "political",
    "war", "conflict", "attack", "attacks",
}

# Common words that can begin a headline but are not named entities.
ENTITY_STOPWORDS = {
    "the", "how", "what", "why", "when", "where",
    "these", "this", "a", "an",
}

# Small synonym groups for common ways a subject may be described.
SUBJECT_ALIASES = {
    "deadlock": {
        "deadlock", "deadlocked", "stalemate", "coalition",
        "majority", "bloc", "parliamentary", "vote", "voting",
        "poll", "polls", "election", "elections",
    },
    "divisions": {
        "division", "divisions", "divide", "divided",
        "polarization", "polarisation", "rift", "rifts",
    },
}


def new_research_metrics() -> dict[str, int]:
    return {
        "candidates_found": 0,
        "rss_candidates": 0,
        "gdelt_candidates": 0,
        "classified_relevant": 0,
        "classified_partially_relevant": 0,
        "classified_irrelevant": 0,
        "classification_failed": 0,
    }

def _parse_gdelt_date(value: str | None) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ")
        return parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def topic_terms(text: str) -> set[str]:
    """Extract meaningful terms from a topic or article headline."""
    words = re.findall(r"[a-z0-9]+", text.casefold())
    return {
        word for word in words
        if len(word) >= 3 and word not in GENERIC_TERMS
    }

def topic_subject_terms(text: str) -> set[str]:
    """Expand distinctive topic words with known subject synonyms."""
    terms = topic_terms(text)
    expanded = set(terms)

    for term in terms:
        expanded.update(SUBJECT_ALIASES.get(term, set()))

    # Preserve explicit subject words even if the generic filter removes them.
    words = set(re.findall(r"[a-z0-9]+", text.casefold()))

    if "election" in words or "elections" in words:
        expanded.update({"election", "elections"})

    return expanded

def topic_entities(text: str) -> set[str]:
    """Identify known entities without relying on capitalization alone."""
    normalized = re.sub(r"[^a-z0-9]+", " ", text.casefold())
    known_entities = {
        "israel",
        "israeli",
        "netanyahu",
        "palestine",
        "palestinian",
        "ukraine",
        "russia",
        "russian",
        "china",
        "chinese",
        "taiwan",
        "iran",
        "iranian",
        "united states",
        "us",
        "donald trump",
        "nato",
        "european union",
    }

    found = set()

    for entity in known_entities:
        pattern = rf"\b{re.escape(entity)}\b"
        if re.search(pattern, normalized):
            found.add(entity)

    return found


def _feed_matches_topic(title: str, query: str) -> bool:
    """Require a shared entity and shared subject for entity-based topics."""
    query_entities = topic_entities(query)
    title_entities = topic_entities(title)

    query_subjects = topic_subject_terms(query)
    title_subjects = topic_subject_terms(title)

    if not query_subjects or not title_subjects:
        return False

    subject_overlap = query_subjects & title_subjects

    if query_entities:
        entity_overlap = query_entities & title_entities

        # Require both a shared entity and a shared subject.
        return bool(entity_overlap) and bool(subject_overlap)

    # For topics without recognized entities, require multiple shared terms.
    query_terms = topic_terms(query)
    title_terms = topic_terms(title)

    return len(query_terms & title_terms) >= 2

def discover_rss_sources(
    query: str,
    limit: int = 10,
) -> list[dict]:
    """Collect and rank Stage A candidates from configured RSS feeds."""
    candidates = {}
    query_terms = topic_terms(query)

    if not query_terms or limit <= 0:
        return []

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

                # Stage A: deterministic entity and subject filter.
                if not _feed_matches_topic(title, query):
                    continue

                url = normalize_url(link)
                if not url:
                    continue

                published_at = parse_published(entry)
                overlap = query_terms & topic_terms(title)
                relevance = len(overlap) / len(query_terms)

                candidate = {
                    "title": title[:1000],
                    "url": url[:2048],
                    "publisher": source[:255],
                    "published_at": published_at,
                    "snippet": (
                        entry.get("summary")
                        or entry.get("description")
                        or None
                    ),
                    "source_type": "rss",
                    "_relevance": relevance,
                }

                existing = candidates.get(url)

                if (
                    existing is None
                    or candidate["_relevance"] > existing["_relevance"]
                ):
                    candidates[url] = candidate

        except Exception:
            logger.exception(
                "RSS research failed for feed: %s",
                source,
            )

    ranked = sorted(
        candidates.values(),
        key=lambda item: (
            item["_relevance"],
            item["published_at"].timestamp()
            if item["published_at"] is not None
            else 0,
        ),
        reverse=True,
    )

    return ranked[:limit]


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
            "published_at":None,
            "snippet": None,
            "source_type": "news",
        })

    return results


async def discover_sources(
    query: str,
    limit: int = 10,
    metrics: dict[str, int] | None = None,
    review_queue: list[dict] | None = None,
) -> list[dict]:
    
    """Discover, classify, and rank sources for a research topic."""

    if metrics is None:
        metrics = new_research_metrics()

    if review_queue is None:
        review_queue = []
   

    candidate_limit = min(limit * 3, 75)

    rss_candidates = await asyncio.to_thread(
        discover_rss_sources,
        query,
        candidate_limit,
    )
    metrics["rss_candidates"] += len(rss_candidates)
    metrics["candidates_found"] += len(rss_candidates)

    accepted = []
    seen_urls = set()

    async def classify_candidates(candidates: list[dict]) -> None:
        for item in candidates:
            url = normalize_url(item["url"])

            if not url or url in seen_urls:
                continue

            seen_urls.add(url)

            
            result = await classify_article_relevance(
                topic=query,
                title=item["title"],
                snippet=item.get("snippet"),
            )

            if result is None:
                metrics["classification_failed"] += 1
                logger.warning("Excluding unclassified article: %s",item["title"])
                continue

            if result.classification == "relevant":
                metrics["classified_relevant"] += 1
                # Keep your existing code that accepts this candidate.

            elif result.classification == "partially_relevant":
                metrics["classified_partially_relevant"] += 1

                logger.info(
                    "Held partial match for review: %s | %s",
                    item["title"],
                    result.rationale,
                )

                review_item = {
                    "title": item["title"],
                    "url": url,
                    "publisher": item.get("publisher"),
                    "published_at": item.get("published_at"),
                    "snippet": item.get("snippet"),
                    "source_type": item.get("source_type"),
                    "classification": "partially_relevant",
                    "rationale": result.rationale,
                }

                review_queue.append(review_item)
                continue

            else:
                metrics["classified_irrelevant"] += 1
                logger.debug("Rejected irrelevant article: %s", item["title"])
                continue
                
            item["url"] = url
            accepted.append(item)

    await classify_candidates(rss_candidates)

    # # Use GDELT as a fallback if RSS did not provide enough
    # # AI-confirmed relevant articles.
    # if len(accepted) < limit:
    #     try:
    #         gdelt_candidates = await discover_gdelt_sources(
    #             query=query,
    #             limit=candidate_limit,
    #         )
    #     except httpx.HTTPStatusError as exc:
    #         if exc.response.status_code == 429:
    #             logger.warning(
    #                 "GDELT rate-limited; using available RSS results"
    #             )
    #             gdelt_candidates = []
    #         else:
    #             raise
    #     except httpx.HTTPError:
    #         logger.exception("GDELT source discovery failed")
    #         gdelt_candidates = []

    #     filtered_gdelt = []
    #     metrics["gdelt_candidates"] += len(filtered_gdelt)
    #     metrics["candidates_found"] += len(filtered_gdelt)

    #     for item in gdelt_candidates:
    #         url = normalize_url(item.get("url", ""))
    #         title = (item.get("title") or "").strip()

    #         if not url or not title:
    #             continue

    #         if url in seen_urls:
    #             continue

    #         if not _feed_matches_topic(title, query):
    #             continue

    #         overlap = topic_terms(query) & topic_terms(title)
    #         item["_relevance"] = (
    #             len(overlap) / len(topic_terms(query))
    #             if topic_terms(query)
    #             else 0
    #         )
    #         item["url"] = url
    #         filtered_gdelt.append(item)

    #     filtered_gdelt.sort(
    #         key=lambda item: (
    #             item["_relevance"],
    #             item["published_at"].timestamp()
    #             if item.get("published_at") is not None
    #             else 0,
    #         ),
    #         reverse=True,
    #     )

    #     await classify_candidates(filtered_gdelt)

    # Rank confirmed relevant articles before publisher diversification.
    accepted.sort(
        key=lambda item: (
            item.get("_relevance", 0),
            item["published_at"].timestamp()
            if item.get("published_at") is not None
            else 0,
        ),
        reverse=True,
    )

    selected = []
    selected_urls = set()
    publisher_counts = {}

    # First pass: prefer one article per publisher.
    for item in accepted:
        publisher = (
            item.get("publisher")
            or urlsplit(item["url"]).netloc
        ).casefold()

        if publisher_counts.get(publisher, 0) >= 1:
            continue

        selected.append(item)
        selected_urls.add(item["url"])
        publisher_counts[publisher] = 1

        if len(selected) >= limit:
            break

    # Second pass: fill remaining slots with the next-best relevant articles.
    if len(selected) < limit:
        for item in accepted:
            if item["url"] in selected_urls:
                continue

            selected.append(item)
            selected_urls.add(item["url"])

            if len(selected) >= limit:
                break

    # Remove internal ranking metadata from the returned records.
    for item in selected:
        item.pop("_relevance", None)

    return selected
