
import pytest
from unittest.mock import AsyncMock

from pydantic import ValidationError
from app.clients.argus_core_client import InferenceResult

from app.schemas import ResearchSynthesis
from app.services.synthesis_service import synthesize_research


@pytest.fixture
def sample_sources():
    return [
        {
            "title": "NATO announces new defense initiative",
            "publisher": "Reuters",
            "published_at": "2026-10-20",
            "snippet": "NATO members announced a new defense initiative.",
            "url": "https://example.com/nato-defense",
        }
    ]


@pytest.fixture
def valid_synthesis():
    return ResearchSynthesis(
        topic="NATO defense initiative",
        executive_summary="NATO announced a new defense initiative.",
        timeline=[],
        key_actors=[],
        question_findings=[
            {
                "question": "What did NATO announce?",
                "finding": "NATO announced a new defense initiative.",
                "evidence": [
                    {
                        "source_title": "NATO announces new defense initiative",
                        "source_url": "https://example.com/nato-defense",
                        "publisher": "Reuters",
                        "claim": "NATO announced a new defense initiative.",
                        "evidence_type": "reported_fact",
                        "confidence": "high",
                    }
                ],
                "unanswered_aspects": [],
            }
        ],
        conflicting_accounts=[],
        evidence_gaps=[],
        limitations=["Only source snippets were analyzed."],
    )


@pytest.mark.asyncio
async def test_synthesize_research_success(
    sample_sources,
    valid_synthesis,
):
    inference = AsyncMock()
    inference.generate_structured.return_value = InferenceResult(
        valid_synthesis.model_dump_json(), "actual-core-model",
    )
    result = await synthesize_research(
        topic="NATO defense initiative",
        objective="Understand the initiative.",
        research_questions=["What did NATO announce?"],
        sources=sample_sources,
        inference_client=inference,
    )

    assert isinstance(result, ResearchSynthesis)
    assert result.topic == "NATO defense initiative"
    assert len(result.question_findings) == 1


@pytest.mark.asyncio
async def test_synthesize_research_rejects_unknown_source_url(
    sample_sources,
    valid_synthesis,
):
    data = valid_synthesis.model_dump()
    data["question_findings"][0]["evidence"][0]["source_url"] = (
        "https://fake-source.com/article"
    )

    inference = AsyncMock()
    inference.generate_structured.return_value = InferenceResult(
        ResearchSynthesis(**data).model_dump_json(), "actual-core-model",
    )
    with pytest.raises(ValueError, match="referenced URLs"):
        await synthesize_research(
            topic="NATO defense initiative",
            objective="Understand the initiative.",
            research_questions=["What did NATO announce?"],
            sources=sample_sources,
            inference_client=inference,
        )



@pytest.mark.asyncio
async def test_synthesize_research_rejects_empty_sources():
    with pytest.raises(ValueError, match="without sources"):
        await synthesize_research(
            topic="NATO defense initiative",
            objective="Understand the initiative.",
            research_questions=["What did NATO announce?"],
            sources=[],
        )


@pytest.mark.asyncio
async def test_synthesize_research_rejects_invalid_model_output(
    sample_sources,
):
    inference = AsyncMock()
    inference.generate_structured.return_value = InferenceResult(
        '{"invalid": "response"}', "actual-core-model",
    )
    with pytest.raises(ValidationError):
        await synthesize_research(
            topic="NATO defense initiative",
            objective="Understand the initiative.",
            research_questions=["What did NATO announce?"],
            sources=sample_sources,
            inference_client=inference,
        )
