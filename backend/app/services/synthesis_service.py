
import json
import logging
from datetime import date, datetime

from app.clients.argus_core_client import CoreInferenceClient
from app.services.research_inference import generate_research_output
from app.schemas import ResearchSynthesis

logger = logging.getLogger(__name__)


async def synthesize_research(
    topic: str,
    objective: str,
    research_questions: list[str],
    sources: list[dict],
    *,
    inference_client: CoreInferenceClient | None = None,
) -> ResearchSynthesis:

    if not sources:
        raise ValueError("Cannot synthesize research without sources.")

    # Keep the prompt manageable for the local model.
    source_payload = []

    for source in sources[:25]:
        published_at = source.get("published_at")
        if isinstance(published_at, (date, datetime)):
            published_at = published_at.isoformat()
        source_payload.append({
            "title": source.get("title", ""),
            "publisher": source.get("publisher", ""),
            "published_at": published_at,
            "snippet": (source.get("snippet") or "")[:1500],
            "url": source.get("url", ""),
        })

    allowed_urls = {
        source["url"]
        for source in source_payload
        if source["url"]
    }

    system_prompt = """
    You are the evidence synthesis engine for Global Chess,
    a geopolitical research platform.

    Your job is to organize collected research into a structured,
    evidence-based report.

    STRICT EVIDENCE RULES:

    1. Use only the supplied source titles, publishers,
       publication dates, snippets, and URLs.

    2. You have NOT read the full articles.
       Never imply that you have.

    3. Never invent facts, dates, actors, quotes,
       events, or causal relationships.

    4. Every evidence item must reference an exact
       URL supplied in the source data.

    5. Distinguish:
       - reported_fact: something directly reported
         in the supplied material.
       - attributed_claim: a statement attributed to
         a person, government, organization, or source.
       - analysis: a clearly labeled inference based
         on the supplied evidence.

    6. Confidence describes how strongly the supplied
       material supports the statement, not whether
       the statement is objectively true.

    7. Do not invent timeline dates. Use only dates
       present in the source data.

    8. Do not manufacture conflicting accounts.
       Identify conflicts only when supplied material
       explicitly presents materially different accounts.

    9. If evidence is insufficient, identify the gap
       rather than guessing.

    10. Treat multiple reports of the same syndicated
        story cautiously. They are not necessarily
        independent corroboration.

    Return only structured JSON matching the supplied schema.
    """

    user_prompt = f"""
    TOPIC:
    {topic}

    RESEARCH OBJECTIVE:
    {objective}

    RESEARCH QUESTIONS:
    {json.dumps(research_questions, ensure_ascii=False)}

    COLLECTED SOURCES:
    {json.dumps(source_payload, ensure_ascii=False, indent=2)}

    Produce a structured research synthesis.

    Address each research question independently.
    Include an executive summary, timeline, key actors,
    evidence, conflicting accounts, evidence gaps,
    and limitations.

    Be conservative. Empty lists are preferable to
    unsupported conclusions.
    """

    logger.info(
        "Synthesizing %s sources for topic: %s",
        len(source_payload),
        topic,
    )

    synthesis = await generate_research_output(
        system_prompt=system_prompt, user_prompt=user_prompt,
        response_model=ResearchSynthesis, timeout=180.0, temperature=0,
        inference_client=inference_client,
    )

    # Verify that the model did not invent source URLs.
    referenced_urls = set()

    for finding in synthesis.question_findings:
        for evidence in finding.evidence:
            referenced_urls.add(evidence.source_url)

    for event in synthesis.timeline:
        referenced_urls.update(event.source_urls)

    for actor in synthesis.key_actors:
        referenced_urls.update(actor.source_urls)

    for conflict in synthesis.conflicting_accounts:
        referenced_urls.update(conflict.source_urls)

    invalid_urls = referenced_urls - allowed_urls

    if invalid_urls:
        raise ValueError(
            f"Synthesis referenced URLs not supplied as sources: "
            f"{invalid_urls}"
        )

    logger.info(
        "Research synthesis completed for: %s",
        topic,
    )

    return synthesis
