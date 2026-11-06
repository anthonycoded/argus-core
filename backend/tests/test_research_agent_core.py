import json
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest

from app.agents import research_agent
from app.clients.argus_core_client import ArgusCoreClient, CoreClientError


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", AsyncMock(
        side_effect=AssertionError("Unexpected live inference")))
    direct = AsyncMock(side_effect=AssertionError("Research called Ollama directly"))
    monkeypatch.setattr("app.services.ollama_service.ollama_client.generate_structured", direct)
    yield
    direct.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario,rounds,status", [
    ("finish", 1, "completed"), ("max_rounds", 3, "incomplete"),
    ("duplicate_queries", 2, "incomplete"), ("empty", 1, "incomplete"),
    ("synthesis_rate_limit", 1, "incomplete"), ("synthesis_timeout", 1, "incomplete"),
    ("synthesis_invalid", 1, "incomplete"),
])
async def test_agent_http_integration_preserves_stopping_and_partial_results(monkeypatch, scenario, rounds, status):
    source = {"title": "Supplied report", "snippet": "Supplied snippet",
              "url": "https://example.test/source", "publisher": "Example", "published_at": None}
    discovery = AsyncMock(return_value=[] if scenario == "empty" else [source])
    monkeypatch.setattr(research_agent, "discover_sources", discovery)
    calls = []
    evaluations = 0
    def respond(request):
        nonlocal evaluations
        payload = json.loads(request.content)
        assert request.headers["X-API-Key"] == "test-only-agent-key"
        properties = payload["response_schema"]["properties"]
        if "objective" in properties:
            operation = "plan"
            result = {"objective": "Investigate", "research_questions": ["What happened?", "Why?"]}
        elif "decision" in properties:
            operation = "evaluate"
            evaluations += 1
            continuing = scenario in {"max_rounds", "duplicate_queries"}
            result = {"decision": "continue" if continuing else "finish", "reasoning": "Supplied evidence",
                      "missing_information": ([f"Gap {evaluations}"] if scenario == "max_rounds" else
                                              ["What happened?"] if continuing else [])}
        else:
            operation = "synthesize"
            if scenario == "synthesis_rate_limit":
                calls.append(operation)
                return httpx.Response(429, headers={"Retry-After": "60"}, text="private error")
            if scenario == "synthesis_timeout":
                calls.append(operation)
                raise httpx.ReadTimeout("private error", request=request)
            result = {"topic": "Topic", "executive_summary": "Supplied evidence only.",
                      "limitations": ["Only snippets were read."], "evidence_gaps": ["Unverified context"]}
            if scenario == "synthesis_invalid":
                result = {"invalid": True}
        calls.append(operation)
        return httpx.Response(200, json={"status": "success", "model": "actual-core-model", "result": json.dumps(result)})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = ArgusCoreClient(base_url="https://core.test", api_key="test-only-agent-key", http_client=http)
        result = await research_agent.run_research_agent("Topic", inference_client=client)
    assert result.rounds_completed == rounds and result.status == status
    assert calls.count("plan") == 1 and calls.count("evaluate") == rounds
    assert calls.count("synthesize") == (0 if scenario == "empty" else 1)
    assert result.sources_discovered == (0 if scenario == "empty" else 1)
    assert len(result.decision_history) == rounds
    queries = [call.kwargs["query"] for call in discovery.call_args_list]
    assert len(queries) == len(set(queries))  # repeated follow-up queries stay suppressed
    if scenario in {"finish", "max_rounds", "duplicate_queries"}:
        assert result.synthesis.limitations == ["Only snippets were read."]
        assert result.synthesis.evidence_gaps == ["Unverified context"]
    if scenario.startswith("synthesis_"):
        assert result.synthesis is None and result.sources == [source]
        assert "Collected 1 unique sources" in result.findings
        assert any("Collected research sources are still available" in text for text in result.limitations)
        assert "private error" not in str(result.model_dump())
    if scenario == "synthesis_rate_limit":
        assert any("Retry-After: 60" in text for text in result.limitations)
    if scenario == "max_rounds":
        assert "Gap 3" in result.limitations


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["plan", "evaluate"])
async def test_agent_stops_on_core_failure_without_extra_inference_or_discovery(monkeypatch, stage):
    discovery = AsyncMock(return_value=[])
    monkeypatch.setattr(research_agent, "discover_sources", discovery)
    requests = []
    def respond(request):
        requests.append(request)
        if stage == "evaluate" and len(requests) == 1:
            return httpx.Response(200, json={"status": "success", "model": "actual-core-model",
                                            "result": json.dumps({"objective": "Investigate", "research_questions": ["Why?"]})})
        return httpx.Response(503, headers={"Retry-After": "120"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = ArgusCoreClient(base_url="https://core.test", api_key="test-only-agent-key", http_client=http)
        with pytest.raises(CoreClientError) as error:
            await research_agent.run_research_agent("Topic", inference_client=client)
    assert error.value.status_code == 503 and error.value.retry_after == "120"
    assert len(requests) == (1 if stage == "plan" else 2)
    assert discovery.await_count == (0 if stage == "plan" else 1)
