"""Global Chess RSS contract adaptation, without editorial filtering or writes."""

from dataclasses import dataclass

from app.clients.argus_core_client import (
    CoreClientError, CoreDiscoveryClient, RSSDiscoveryError, validated_retry_after,
)
from app.clients.rss_discovery_contracts import RSSCandidate
from app.services.rss_service import normalize_url


@dataclass
class IngestionObservation:
    candidate: RSSCandidate
    provider_metadata: dict


@dataclass
class IngestionFeed:
    source: str
    observations: list[IngestionObservation]
    error: str | None = None


def _safe_error(error: CoreClientError) -> str:
    # Only our client's known constants may cross the HTTP response boundary.
    known = {
        "Argus-Core authentication failed.", "Argus-Core discovery permission denied.",
        "Argus-Core returned an invalid RSS discovery response.",
        "Argus-Core returned inconsistent RSS entry counts.",
        "RSS feed is incomplete; some entries or metadata could not be accepted.",
        "RSS snapshot exceeds the pagination limit.",
    }
    detail = error.detail if error.detail in known else {
        410: "RSS snapshot expired; coverage is incomplete.",
        429: "RSS discovery rate limit exceeded.",
        503: "RSS discovery service unavailable.",
        504: "RSS discovery timed out.",
        422: "RSS feed configuration is invalid.",
    }.get(error.status_code, "RSS discovery failed; coverage is incomplete.")
    retry = validated_retry_after(error.retry_after)
    if retry is not None:
        detail += f" Retry-After: {retry}."
    return detail


async def collect_ingestion_feed(
    source: str, feed_id: str, client: CoreDiscoveryClient,
) -> IngestionFeed:
    error = None
    try:
        sources = await client.discover_rss(feed_id=feed_id)
    except RSSDiscoveryError as exc:
        sources = exc.partial_sources
        error = _safe_error(exc)
    except CoreClientError as exc:
        sources = []
        error = _safe_error(exc)
    except Exception:
        sources = []
        error = "RSS discovery failed; coverage is incomplete."

    observations = []
    invalid = False
    # Also bound injected implementations; never silently truncate a feed.
    if not isinstance(sources, list) or len(sources) > 500:
        return IngestionFeed(source, [], "RSS candidate limit exceeded; coverage is incomplete.")
    for item in sources:
        try:
            metadata = item["provider_metadata"]
            candidate = RSSCandidate.model_validate({
                **item, "title": item["title"].strip(),
                "provider": metadata["provider"], "canonical_url": metadata["canonical_url"],
                "domain": metadata["domain"], "provenance": metadata["provenance"],
            })
            if "\x00" in candidate.title or len(normalize_url(candidate.url)) > 2048:
                raise ValueError("Candidate cannot be stored as a Topic")
            observations.append(IngestionObservation(candidate, dict(metadata)))
        except (KeyError, TypeError, ValueError, AttributeError):
            invalid = True
    if invalid:
        validation_error = "Invalid RSS candidates were rejected; coverage is incomplete."
        error = (error + " " + validation_error)[:300] if error else validation_error
    return IngestionFeed(source, observations, error)
