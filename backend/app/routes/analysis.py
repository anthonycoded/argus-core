
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.clients.argus_core_client import CoreInferenceClient, get_core_inference_client
from app.database import get_db
from app.models import Topic, TopicAnalysis
from app.services.ollama_service import analyze_topic_with_ollama

router = APIRouter(prefix="/topics", tags=["Analysis"])


@router.post("/{topic_id}/analyze")
async def analyze_topic(
    topic_id: int,
    db: Session = Depends(get_db),
    inference_client: CoreInferenceClient = Depends(get_core_inference_client),
):
    topic = db.get(Topic, topic_id)

    if topic is None:
        raise HTTPException(
            status_code=404,
            detail="Topic not found",
        )

    generated = await analyze_topic_with_ollama(
        title=topic.title,
        source=topic.source,
        published_at=topic.published_at,
        summary=topic.summary,
        url=topic.url,
        inference_client=inference_client,
    )
    result = generated.analysis

    analysis = (
        db.query(TopicAnalysis)
        .filter_by(topic_id=topic.id)
        .first()
    )

    if analysis is None:
        analysis = TopicAnalysis(topic_id=topic.id)
        db.add(analysis)

    analysis.summary = result.summary
    analysis.relevance = result.relevance
    analysis.research_priority = result.research_priority
    analysis.rationale = result.rationale
    analysis.research_questions = json.dumps(
        result.research_questions
    )
    analysis.model = generated.model

    try:
        db.commit()
        db.refresh(analysis)
    except Exception:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Database write failed while saving analysis",
        )

    return {
        "topic_id": topic.id,
        "title": topic.title,
        "analysis": result.model_dump(),
        "model": generated.model,
    }
