import logging

import httpx

from app.config import OLLAMA_BASE_URL, OLLAMA_MODEL
from app.schemas import ResearchPlan, ResearchDecision

logger = logging.getLogger(__name__)


async def generate_research_plan(
    topic: str,
) -> ResearchPlan:

    system_prompt = """
    You are a geopolitical research planner.

    Your job is to create a structured research plan
    for investigating a geopolitical news topic.

    Generate:
    1. A clear research objective.
    2. Between 3 and 8 specific research questions.

    Questions should investigate different aspects
    of the topic, including relevant actors,
    historical context, events, and consequences.

    Return valid JSON only.
    """

    user_prompt = f"""
    Research topic: {topic}

    Create a research plan.
    """

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "stream": False,
                "format": ResearchPlan.model_json_schema(),
                "messages": [
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user",
                        "content": user_prompt,
                    },
                ],
                "options": {
                    "temperature": 0.2,
                },
            },
        )

        response.raise_for_status()

    content = response.json()["message"]["content"]

    return ResearchPlan.model_validate_json(content)


async def evaluate_research_progress(
    topic: str,
    questions: list[str],
    sources: list[dict],
    round_number: int,
    max_rounds: int = 3,
) -> ResearchDecision:

    if round_number >= max_rounds:
        return ResearchDecision(
            decision="finish",
            reasoning="Maximum research rounds reached.",
            missing_information=[],
        )

    source_summaries = [
        {
            "title": source.get("title"),
            "publisher": source.get("publisher"),
            "snippet": source.get("snippet"),
        }
        for source in sources
    ]

    system_prompt = """
    You are a geopolitical research evaluator.

    Evaluate whether the available sources provide
    enough information to address the research questions.

    Choose:
    - continue: important questions remain unanswered.
    - finish: sufficient evidence has been collected.

    Do not assume information exists when it is not
    present in the supplied sources.

    Return valid JSON only.
    """

    user_prompt = f"""
    Topic:
    {topic}

    Research questions:
    {questions}

    Available sources:
    {source_summaries}

    Should research continue?
    """

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            f"{OLLAMA_BASE_URL}/api/chat",
            json={
                "model": OLLAMA_MODEL,
                "stream": False,
                "format": ResearchDecision.model_json_schema(),
                "messages": [
                    {
                        "role": "system",
                        "content": system_prompt,
                    },
                    {
                        "role": "user",
                        "content": user_prompt,
                    },
                ],
                "options": {
                    "temperature": 0,
                },
            },
        )

        response.raise_for_status()

    content = response.json()["message"]["content"]

    return ResearchDecision.model_validate_json(content)