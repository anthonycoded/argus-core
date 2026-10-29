
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Topic
from app.schemas import TopicStatusUpdate

router = APIRouter(prefix="/topics", tags=["Topics"])


@router.get("")
def list_topics(
    status: str | None = None,
    min_score: float = Query(0, ge=0, le=100),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    query = select(Topic).where(Topic.score >= min_score)

    if status:
        allowed = {"candidate", "approved", "rejected"}

        if status not in allowed:
            raise HTTPException(
                status_code=400,
                detail=f"status must be one of {sorted(allowed)}",
            )

        query = query.where(Topic.status == status)

    query = query.order_by(
        Topic.score.desc(),
        Topic.fetched_at.desc(),
    ).limit(limit)

    topics = db.scalars(query).all()

    return [
        {
            "id": topic.id,
            "title": topic.title,
            "url": topic.url,
            "source": topic.source,
            "published_at": topic.published_at,
            "score": topic.score,
            "relevance_score": topic.relevance_score,
            "freshness_score": topic.freshness_score,
            "status": topic.status,
        }
        for topic in topics
    ]


@router.patch("/{topic_id}/status")
def update_topic_status(
    topic_id: int,
    payload: TopicStatusUpdate,
    db: Session = Depends(get_db),
):
    if payload.status not in {"approved", "rejected", "candidate"}:
        raise HTTPException(
            status_code=400,
            detail="Use approved, rejected, or candidate",
        )

    topic = db.get(Topic, topic_id)

    if topic is None:
        raise HTTPException(
            status_code=404,
            detail="Topic not found",
        )

    topic.status = payload.status
    db.commit()

    return {
        "id": topic.id,
        "status": topic.status,
        "message": "Topic status updated",
    }
