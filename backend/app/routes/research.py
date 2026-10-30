
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
import httpx

from app.database import get_db
from app.models import ResearchSource, Topic
from app.services.research_service import (
    discover_sources,
    new_research_metrics,
)
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/topics", tags=["Research"])


@router.post("/{topic_id}/research")
async def research_topic(
    topic_id: int,
    limit: int = Query(10, ge=1, le=25),
    db: Session = Depends(get_db),
):
    topic = db.get(Topic, topic_id)

    if topic is None:
        raise HTTPException(
            status_code=404,
            detail="Topic not found",
        )

    if topic.status != "approved":
        raise HTTPException(
            status_code=409,
            detail="Approve the topic before starting research",
        )

    
    try:
        metrics = new_research_metrics()

        review_queue = []

        discovered = await discover_sources(
            query=topic.title,
            limit=limit,
            metrics=metrics,
            review_queue=review_queue,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail="Research providers are temporarily unavailable.",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="A research provider returned invalid data.",
        ) from exc


    inserted = 0
    updated = 0

    try:
        for item in discovered:
            existing = db.scalar(
                select(ResearchSource).where(
                    ResearchSource.topic_id == topic.id,
                    ResearchSource.url == item["url"],
                )
            )

            if existing:
                existing.title = item["title"]
                existing.publisher = item["publisher"]
                existing.published_at = item["published_at"]
                updated += 1
            else:
                db.add(
                    ResearchSource(
                        topic_id=topic.id,
                        **item,
                    )
                )
                inserted += 1

        db.commit()

    except Exception as exc:
        db.rollback()
        logger.exception("Failed to save research sources")

        raise HTTPException(
            status_code=500,
            detail="Failed to save research sources",
        ) from exc

    saved_sources = db.scalars(
        select(ResearchSource)
        .where(ResearchSource.topic_id == topic.id)
        .order_by(ResearchSource.discovered_at.desc())
    ).all()

    return {
        "topic_id": topic.id,
        "topic_title": topic.title,
        "discovered": len(discovered),
        "inserted": inserted,
        "updated": updated,
        **metrics,
        "total_saved": len(saved_sources),
        "review_queue": review_queue,
        "sources": [
            {
                "id": source.id,
                "title": source.title,
                "url": source.url,
                "publisher": source.publisher,
                "published_at": source.published_at,
                "source_type": source.source_type,
            }
            for source in saved_sources
        ],
    }


@router.get("/{topic_id}/sources")
def list_research_sources(
    topic_id: int,
    db: Session = Depends(get_db),
):
    topic = db.get(Topic, topic_id)

    if topic is None:
        raise HTTPException(
            status_code=404,
            detail="Topic not found",
        )

    sources = db.scalars(
        select(ResearchSource)
        .where(ResearchSource.topic_id == topic_id)
        .order_by(ResearchSource.discovered_at.desc())
    ).all()

    return [
        {
            "id": source.id,
            "topic_id": source.topic_id,
            "title": source.title,
            "url": source.url,
            "publisher": source.publisher,
            "published_at": source.published_at,
            "snippet": source.snippet,
            "source_type": source.source_type,
            "discovered_at": source.discovered_at,
         
        }
        for source in sources
    ]
