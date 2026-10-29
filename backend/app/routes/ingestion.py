
from datetime import datetime, timezone

import feedparser
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import FEEDS
from app.database import get_db
from app.models import Topic
from app.services.rss_service import (
    normalize_url,
    parse_published,
    score_topic,
)

router = APIRouter(tags=["Ingestion"])


@router.post("/ingest")
def ingest_topics(db: Session = Depends(get_db)):
    inserted = 0
    updated = 0
    feed_errors = []

    for source, feed_url in FEEDS.items():
        try:
            feed = feedparser.parse(feed_url)

            if getattr(feed, "bozo", False) and not feed.entries:
                feed_errors.append({
                    "source": source,
                    "error": "Feed could not be parsed",
                })
                continue

            for entry in feed.entries:
                title = (entry.get("title") or "").strip()
                link = (entry.get("link") or "").strip()

                if not title or not link:
                    continue

                url = normalize_url(link)
                published_at = parse_published(entry)

                relevance, freshness, score = score_topic(
                    title,
                    published_at,
                )

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
                    updated += 1
                else:
                    db.add(
                        Topic(
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
                    )
                    inserted += 1

        except Exception as exc:
            feed_errors.append({
                "source": source,
                "error": str(exc)[:300],
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
