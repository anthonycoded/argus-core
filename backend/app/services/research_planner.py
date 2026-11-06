import logging

from app.clients.argus_core_client import CoreInferenceClient
from app.services.research_inference import generate_research_output
from app.schemas import ResearchPlan, ResearchDecision

logger = logging.getLogger(__name__)


async def generate_research_plan(
    topic: str,
    *,
    inference_client: CoreInferenceClient | None = None,
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

    return await generate_research_output(
        system_prompt=system_prompt, user_prompt=user_prompt,
        response_model=ResearchPlan, timeout=120.0, temperature=0.2,
        inference_client=inference_client,
    )



async def evaluate_research_progress(
    topic: str,
    questions: list[str],
    sources: list[dict],
    round_number: int,
    max_rounds: int = 3,
    *,
    inference_client: CoreInferenceClient | None = None,
) -> ResearchDecision:

    source_summaries = [
        {
            "title": source.get("title"),
            "publisher": source.get("publisher"),
            "snippet": source.get("snippet"),
        }
        for source in sources
    ]

    is_final_round = round_number >= max_rounds

    system_prompt = """
    You are a rigorous geopolitical research evaluator
    for Global Chess.

    Your job is to determine whether collected sources
    provide enough evidence to address each research question.

    Evaluate every research question independently.

    For each question, determine whether the supplied sources:
    - Directly provide an answer.
    - Provide only partial information.
    - Provide no meaningful information.

    DECISION RULES:

    Choose "finish" when:
    - The available evidence adequately addresses the
      important research questions.
    - Additional searches are unlikely to materially
      improve the research collection.

    Choose "continue" when:
    - Important research questions remain unanswered.
    - Important actors, events, causes, or consequences
      lack supporting sources.
    - More targeted research could improve coverage.

    MISSING INFORMATION:

    - List specific unanswered or inadequately supported
      research questions.
    - Do not list questions that are already adequately
      addressed by the supplied evidence.
    - Never invent answers or assume information exists.

    IMPORTANT:
    - A large number of sources does not automatically
      mean the research is complete.
    - Multiple articles repeating the same information
      do not necessarily provide independent corroboration.
    - A source title or short snippet may be insufficient
      to answer a research question.
    - Distinguish evidence from assumptions.
    - Do not treat a lack of information as proof.

    Return only the requested structured classification.
    """

    user_prompt = f"""
    Research topic:
    {topic}

    Research round:
    {round_number} of {max_rounds}

    Is this the final permitted research round?
    {is_final_round}

    Research questions:
    {questions}

    Available sources:
    {source_summaries}

    Evaluate the evidence and decide whether research
    should continue.

    If this is the final round, choose "finish" because
    no further rounds are available, but still list every
    important unanswered question in missing_information.

    If research is sufficiently complete, use an empty
    missing_information list.
    """

    return await generate_research_output(
        system_prompt=system_prompt, user_prompt=user_prompt,
        response_model=ResearchDecision, timeout=120.0, temperature=0,
        inference_client=inference_client,
    )
