import logging

from app.schemas import ResearchAgentResult
from app.services.research_planner import (
    generate_research_plan,
    evaluate_research_progress,
)
from app.services.research_service import (
    discover_sources,
    new_research_metrics,
)

logger = logging.getLogger(__name__)


async def run_research_agent(
    topic: str,
    max_rounds: int = 3,
    sources_per_query: int = 5,
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

    # --------------------------------------------------
    # 1. Generate research plan
    # --------------------------------------------------

    plan = await generate_research_plan(topic)

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

            discovered = await discover_sources(
                query=query,
                limit=sources_per_query,
                metrics=metrics,
                review_queue=review_queue,
                processed_urls=processed_urls,
            )

            for source in discovered:

                url = source.get("url")

                if url:
                    all_sources[url] = source

        current_sources = list(all_sources.values())

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
    else:
        status = "completed"

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
    )