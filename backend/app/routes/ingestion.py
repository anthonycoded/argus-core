
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.argus_core_client import CoreDiscoveryClient, get_core_discovery_client
from app.global_chess_config import INGESTION_RSS_FEEDS
from app.database import get_db
from app.models import Topic
from app.services.rss_service import (
    normalize_url,
    score_topic,
)
from app.services.ingestion_discovery import IngestionFeed, collect_ingestion_feed

router = APIRouter(tags=["Ingestion"])


@router.post("/ingest")
async def ingest_topics(
    db: Session = Depends(get_db),
    discovery_client: CoreDiscoveryClient = Depends(get_core_discovery_client),
):
    feeds = []
    for source, feed_id in INGESTION_RSS_FEEDS:
        feeds.append(await collect_ingestion_feed(source, feed_id, discovery_client))
    return await run_in_threadpool(_persist_topics, feeds, db)


def _persist_topics(feeds: list[IngestionFeed], db: Session):
    inserted = 0
    updated = 0
    feed_errors = []
    # Include pending inserts: SELECT cannot see them with autoflush disabled.
    topics_by_url: dict[str, Topic] = {}

    for feed in feeds:
        source = feed.source
        if feed.error:
            feed_errors.append({"source": source, "error": feed.error})
        try:
            for observation in feed.observations:
                title = observation.candidate.title
                url = normalize_url(observation.candidate.url)
                published_at = observation.candidate.published_at

                relevance, freshness, score = score_topic(
                    title,
                    published_at,
                )

                existing = topics_by_url.get(url)
                first_observation = existing is None
                if first_observation:
                    existing = db.scalar(
                        select(Topic).where(Topic.url == url)
                    )

                if existing:
                    # Preserve existing editorial status and summary.
                    existing.title = title[:500]
                    existing.source = source
                    existing.published_at = published_at
                    existing.fetched_at = datetime.now(timezone.utc)
                    existing.relevance_score = relevance
                    existing.freshness_score = freshness
                    existing.score = score
                    if first_observation:
                        updated += 1
                else:
                    existing = Topic(
                        title=title[:500],
                        url=url,
                        source=source,
                        published_at=published_at,
                        relevance_score=relevance,
                        freshness_score=freshness,
                        score=score,
                        status="candidate",
                        summary=None,
                    )
                    db.add(existing)
                    inserted += 1
                topics_by_url[url] = existing

        except Exception:
            feed_errors.append({
                "source": source,
                "error": "Topic processing failed; feed coverage is incomplete.",
            })

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Database write failed during ingestion",
        ) from exc

    return {
        "inserted": inserted,
        "updated": updated,
        "feed_errors": feed_errors,
    }
