
import asyncio
import calendar
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx
import re

from app.clients.argus_core_client import CoreClientError, CoreDiscoveryClient, RSSDiscoveryError, get_core_discovery_client
from app.global_chess_config import RESEARCH_RSS_FEEDS
from app.services.rss_service import normalize_url
from app.services.ollama_service import classify_article_relevance
from app.services.classification_coverage import ClassificationContext, ClassificationFailure
from app.clients.classification_contracts import CoverageSource

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
        "google_news_candidates": 0,
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

async def discover_rss_sources(
    query: str,
    limit: int = 10,
    *,
    discovery_client: CoreDiscoveryClient | None = None,
) -> list[dict]:
    """Collect immutable Core snapshots, then apply Global Chess editorial policy."""
    candidates = {}
    query_terms = topic_terms(query)
    if not query_terms or limit <= 0:
        return []
    client = discovery_client or get_core_discovery_client()
    provider_error = None
    coverage_errors = []
    for source, feed_id in RESEARCH_RSS_FEEDS:
        try:
            observations = await client.discover_rss(feed_id=feed_id)
        except CoreClientError as exc:
            observations = exc.partial_sources if isinstance(exc, RSSDiscoveryError) else []
            coverage_errors.append(f"{source}: {exc.detail}")
            if provider_error is None:
                provider_error = exc
        # Collection completes (or explicitly fails) before any topic filtering.
        # Duplicate observations survive transport; winner selection stays here.
        for item in observations:
            title = item["title"].strip()
            if not _feed_matches_topic(title, query):
                continue
            url = normalize_url(item["url"])
            if not url:
                continue
            overlap = query_terms & topic_terms(title)
            candidate = {
                **item, "title": title[:1000], "url": url,
                "publisher": source, "_relevance": len(overlap) / len(query_terms),
            }
            existing = candidates.get(url)
            if existing is None or candidate["_relevance"] > existing["_relevance"]:
                candidates[url] = candidate
    ranked = sorted(
        candidates.values(),
        key=lambda item: (
            item["_relevance"],
            item["published_at"].timestamp() if item["published_at"] is not None else 0,
        ),
        reverse=True,
    )
    selected = ranked[:limit]
    if provider_error is not None:
        error = CoreClientError(provider_error.status_code, "; ".join(coverage_errors), provider_error.retry_after)
        raise ResearchDiscoveryError(error, selected) from None
    return selected


class ResearchDiscoveryError(CoreClientError):
    """Explicit discovery failure carrying already accepted evidence."""

    def __init__(self, error: CoreClientError, partial_sources: list[dict], *, classification_coverage=None):
        super().__init__(error.status_code, error.detail, error.retry_after)
        self.partial_sources = partial_sources
        self.classification_coverage = classification_coverage


async def discover_google_news_sources(
    query: str,
    limit: int = 10,
    *,
    discovery_client: CoreDiscoveryClient | None = None,
) -> list[dict]:
    """Adapt authenticated Core candidates into Global Chess research records."""
    result = await (discovery_client or get_core_discovery_client()).discover_google_news(
        query=query, limit=limit,
    )
    return result.research_sources()


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
    processed_urls: set[str] | None = None,
    *,
    discovery_client: CoreDiscoveryClient | None = None,
    inference_client=None,
    classification_context: ClassificationContext | None = None,
) -> list[dict]:
    
    """Discover, classify, and rank sources for a research topic."""

    if metrics is None:
        metrics = new_research_metrics()

    if review_queue is None:
        review_queue = classification_context.review_queue if classification_context is not None else []
   

    candidate_limit = min(limit * 3, 75)
    context = classification_context if classification_context is not None else ClassificationContext()
    context.review_queue = review_queue
    if context.limit() or context.stop_reason:
        if not context.stop_reason:
            context.stop(context.limit())
        raise ResearchDiscoveryError(context.error, [], classification_coverage=context.snapshot())

    provider_error = None
    try:
        rss_candidates = await discover_rss_sources(
            query, candidate_limit, discovery_client=discovery_client,
        )
    except asyncio.CancelledError:
        context.stop("cancelled", CoreClientError(503, "Research classification was cancelled."))
        raise
    except ResearchDiscoveryError as exc:
        provider_error = exc
        rss_candidates = exc.partial_sources
    metrics["rss_candidates"] += len(rss_candidates)
    metrics["candidates_found"] += len(rss_candidates)

    accepted = []

    seen_urls = (
        processed_urls
        if processed_urls is not None
        else set()
    )

    reviewed_urls = {
        normalize_url(item.get("url", ""))
        for item in review_queue
        if item.get("url")
    }

    # Previously reviewed partial matches should not be
    # classified again in subsequent research rounds.
    seen_urls.update(reviewed_urls)
    observed_urls = set()  # Failed URLs retain their winner within this query only.

    async def classify_candidates(candidates: list[dict]) -> None:
        if len(candidates) > 75:
            raise CoreClientError(502, "Discovery returned an invalid candidate batch.")
        eligible = []
        for item in candidates:
            url = normalize_url(item["url"])
            if not url or url in seen_urls or url in observed_urls:
                continue
            observed_urls.add(url)
            observation = {**item, "url": url}
            eligible.append((observation, CoverageSource.from_candidate(observation)))

        def retain_remaining(offset, reason):
            for _, source in eligible[offset:]:
                context.record(source, reason=reason)

        for index, (item, source) in enumerate(eligible):
            reason = context.stop_reason or context.limit()
            if reason:
                if not context.stop_reason:
                    context.stop(reason)
                retain_remaining(index, reason)
                break

            context.evaluated += 1
            started = context.clock()
            diagnostics = {}
            failure = None
            try:
                async with asyncio.timeout(context.max_seconds - context.elapsed):
                    result = await classify_article_relevance(
                        topic=query, title=item["title"], snippet=item.get("snippet"),
                        inference_client=inference_client, diagnostics=diagnostics,
                    )
                if result is None:
                    raise ClassificationFailure(CoreClientError(502, "Argus-Core returned an invalid relevance classification."),
                                                reason="invalid_output", attempted=True, systemic=False)
            except asyncio.CancelledError as cancelled:
                try:
                    context.stop("cancelled", CoreClientError(503, "Research classification was cancelled."))
                    context.record(source, reason="cancelled", attempted=True)
                    retain_remaining(index + 1, "cancelled")
                finally:
                    raise cancelled  # Preserve cancellation even if accounting fails.
            except TimeoutError:
                failure = ClassificationFailure(CoreClientError(504, "Research classification time limit reached."),
                                                reason="workload_deadline", attempted=True, systemic=True)
            except ClassificationFailure as exc:
                failure = exc
            finally:
                context.elapsed += max(0.0, context.clock() - started)
                if "model" in diagnostics:
                    context.models.append(diagnostics["model"])
            if failure is not None:
                context.record(source, reason=failure.reason, attempted=failure.attempted)
                metrics["classification_failed"] += 1
                context.error = context.error or failure
                if failure.systemic:
                    context.stop(failure.reason, failure)
                    retain_remaining(index + 1, failure.reason)
                    break
                continue

            context.attempted += 1
            context.classified += 1
            url = item["url"]
            # Only successful classifications suppress later explicit rounds.
            seen_urls.add(item["url"])

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

                if "provider_metadata" in item:
                    review_item["provider_metadata"] = item["provider_metadata"]

                if url not in reviewed_urls:
                    review_queue.append(review_item)
                    reviewed_urls.add(url)

                continue

            else:
                metrics["classified_irrelevant"] += 1
                logger.debug("Rejected irrelevant article: %s", item["title"])
                continue
                
            accepted.append(item)
            context.completed_sources.append(item)

    await classify_candidates(rss_candidates)

    if not context.stop_reason and context.limit():
        context.stop(context.limit())

    try:
        google_news_candidates = [] if context.stop_reason else await discover_google_news_sources(
            query=query,
            limit=candidate_limit,
            discovery_client=discovery_client,
        )
    except asyncio.CancelledError:
        context.stop("cancelled", CoreClientError(503, "Research classification was cancelled."))
        raise
    except CoreClientError as exc:
        # Preserve RSS selection, but never disguise a failed request as empty.
        if provider_error is None:
            provider_error = exc
        else:
            provider_error = CoreClientError(
                provider_error.status_code, provider_error.detail + "; Google News: " + exc.detail,
                provider_error.retry_after,
            )
        google_news_candidates = []

    metrics["google_news_candidates"] += len(
        google_news_candidates
    )
    metrics["candidates_found"] += len(
        google_news_candidates
    )

    await classify_candidates(google_news_candidates)

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

    classification_incomplete = bool(context.unclassified or context.stop_reason)
    if provider_error is not None and not context.stop_reason:
        context.stop("discovery_incomplete", provider_error)
    coverage = context.snapshot()
    if provider_error is not None or not coverage.complete:
        error = context.error if context.stop_reason not in {None, "discovery_incomplete"} else provider_error or context.error
        raise ResearchDiscoveryError(
            error, selected,
            classification_coverage=coverage if classification_incomplete else None,
        ) from None
    return selected
