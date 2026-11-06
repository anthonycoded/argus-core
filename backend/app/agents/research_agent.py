import logging

from app.clients.argus_core_client import CoreClientError, CoreDiscoveryClient, CoreInferenceClient, get_core_inference_client
from app.schemas import ResearchAgentResult
from app.services.synthesis_service import synthesize_research
from app.services.research_planner import (
    generate_research_plan,
    evaluate_research_progress,
)
from app.services.research_service import (
    discover_sources,
    new_research_metrics,
    ResearchDiscoveryError,
)

logger = logging.getLogger(__name__)


async def run_research_agent(
    topic: str,
    max_rounds: int = 3,
    sources_per_query: int = 5,
    *,
    inference_client: CoreInferenceClient | None = None,
    discovery_client: CoreDiscoveryClient | None = None,
) -> ResearchAgentResult:

    if not topic.strip():
        raise ValueError("Research topic cannot be empty.")

    if max_rounds < 1:
        raise ValueError("max_rounds must be at least 1.")

    logger.info("Starting research agent for: %s", topic)

    metrics = new_research_metrics()
    review_queue = []

    all_sources = {}
    processed_urls = set()
    executed_queries = set()

    decision_history = []
    limitations = []
    discovery_failed = False

    # --------------------------------------------------
    # 1. Generate research plan
    # --------------------------------------------------

    inference_client = inference_client or get_core_inference_client()
    plan = await generate_research_plan(topic, inference_client=inference_client)

    research_questions = plan.research_questions

    # Initial research uses the original topic.
    search_queries = [topic]

    rounds_completed = 0
    final_decision = None

    # --------------------------------------------------
    # 2. Autonomous research loop
    # --------------------------------------------------

    for round_number in range(1, max_rounds + 1):

        logger.info(
            "Research round %s/%s",
            round_number,
            max_rounds,
        )

        rounds_completed = round_number

        # --------------------------------------------------
        # 3. Discover sources
        # --------------------------------------------------

        for query in search_queries:

            normalized_query = " ".join(query.casefold().split())

            if not normalized_query:
                continue

            if normalized_query in executed_queries:
                logger.info("Skipping previously executed query: %s", query)
                continue

            # Mark query as executed before searching.
            executed_queries.add(normalized_query)

            logger.info("Searching for: %s", query)

            try:
                discovered = await discover_sources(
                    query=query,
                    limit=sources_per_query,
                    metrics=metrics,
                    review_queue=review_queue,
                    processed_urls=processed_urls,
                    discovery_client=discovery_client,
                )
            except ResearchDiscoveryError as exc:
                discovered = exc.partial_sources
                discovery_failed = True
                detail = "Research discovery incomplete: " + exc.detail
                if exc.retry_after is not None:
                    detail += f" Retry-After: {exc.retry_after}."
                limitations.append(detail)

            for source in discovered:

                url = source.get("url")

                if url:
                    all_sources[url] = source

            if discovery_failed:
                break

        current_sources = list(all_sources.values())

        if discovery_failed:
            break  # Preserve evidence; do not issue more Core requests after failure.

        logger.info(
            "Total unique sources discovered: %s",
            len(current_sources),
        )

        # --------------------------------------------------
        # 4. Evaluate research progress
        # --------------------------------------------------

        final_decision = await evaluate_research_progress(
            topic=topic,
            questions=research_questions,
            sources=current_sources,
            round_number=round_number,
            max_rounds=max_rounds,
            inference_client=inference_client,
        )

        decision_history.append({
            "round": round_number,
            "decision": final_decision.decision,
            "reasoning": final_decision.reasoning,
            "missing_information": (
                final_decision.missing_information
            ),
            "sources_collected": len(current_sources),
        })

        logger.info(
            "Agent decision: %s",
            final_decision.decision,
        )

        # --------------------------------------------------
        # 5. Decide whether to continue
        # --------------------------------------------------

        if final_decision.decision == "finish":
            break

        missing_information = (
            final_decision.missing_information
        )

        if not missing_information:
            logger.info(
                "Evaluator did not provide follow-up questions. "
                "Using unanswered research questions."
            )

            missing_information = research_questions[:]

        # Exclude queries already executed in previous rounds.
        search_queries = []
        seen_queries = set()

        for question in missing_information:
            normalized_query = " ".join(question.casefold().split())

            if not normalized_query:
                continue

            if normalized_query in executed_queries:
                continue

            if normalized_query in seen_queries:
                continue

            search_queries.append(question)
            seen_queries.add(normalized_query)

            if len(search_queries) >= 2:
                break

        if not search_queries:
            limitations.append(
                "No new research queries were available."
            )
            break

    # --------------------------------------------------
    # 6. Build research result
    # --------------------------------------------------

    final_sources = list(all_sources.values())


    if not final_sources:
        status = "incomplete"
        limitations.append(
            "No sources passed the relevance classification."
        )

    elif final_decision is None:
        status = "incomplete"
        limitations.append(
            "Research progress could not be evaluated."
        )

    elif final_decision.missing_information:
        status = "incomplete"
        limitations.extend(
            final_decision.missing_information
        )

    elif final_decision.decision != "finish":
        status = "incomplete"
        limitations.append(
            "Research stopped before all important questions "
            "were adequately addressed."
        )

    else:
        status = "completed"
            # --------------------------------------------------
    # 7. Synthesize collected evidence
    # --------------------------------------------------

    synthesis = None

    if final_sources and not discovery_failed:
        try:
            synthesis = await synthesize_research(
                topic=topic,
                objective=plan.objective,
                research_questions=research_questions,
                sources=final_sources,
                inference_client=inference_client,
            )

            logger.info(
                "Evidence synthesis completed for: %s",
                topic,
            )

        except Exception as exc:
            if isinstance(exc, CoreClientError):
                logger.warning("Evidence synthesis failed: %s", exc.detail)
                detail = exc.detail
                if exc.retry_after is not None:
                    detail += f" Retry-After: {exc.retry_after}."
                limitations.append(detail)
            else:
                logger.exception(
                    "Evidence synthesis failed for: %s",
                    topic,
                )

            limitations.append(
                "Evidence synthesis failed. "
                "Collected research sources are still available."
            )

            if status == "completed":
                status = "incomplete"


    if synthesis:
        findings = synthesis.executive_summary
    else:
        findings = (
            f"Research collection finished after "
            f"{rounds_completed} round(s). "
            f"Collected {len(final_sources)} unique sources. "
            "These results represent collected evidence, "
            "not a verified final geopolitical analysis."
        )

    return ResearchAgentResult(
        topic=topic,
        objective=plan.objective,
        status=status,
        rounds_completed=rounds_completed,
        research_questions=research_questions,
        sources_discovered=len(final_sources),
        sources=final_sources,
        metrics=metrics,
        review_queue=review_queue,
        decision_history=decision_history,
        findings=findings,
        limitations=limitations,
        synthesis=synthesis,
    )
