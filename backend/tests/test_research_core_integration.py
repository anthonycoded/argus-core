"""Offline research integration: HTTP mocks, in-memory auth, no discovery I/O."""

import json
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest
from pydantic import ValidationError

from app.clients.argus_core_client import ArgusCoreClient, CoreClientError, InferenceResult
from app.schemas import ResearchPlan, ResearchDecision, ResearchSynthesis
from app.services.research_planner import generate_research_plan, evaluate_research_progress
from app.services.synthesis_service import synthesize_research
from app.services.research_inference import generate_research_output

SOURCE = {"title": "Supplied report", "publisher": "Example", "snippet": "An announcement.",
          "url": "https://example.test/report", "published_at": None}
PLAN = {"objective": "Investigate", "research_questions": ["What happened?"]}
DECISION = {"decision": "finish", "reasoning": "Evidence collected", "missing_information": []}
SYNTHESIS = {"topic": "Topic", "executive_summary": "Supplied material only.",
             "limitations": ["Only snippets were available."]}
OUTPUTS = {"plan": PLAN, "evaluate": DECISION, "synthesize": SYNTHESIS}
MODELS = {"plan": ResearchPlan, "evaluate": ResearchDecision, "synthesize": ResearchSynthesis}


@pytest.fixture(autouse=True)
def forbid_live_requests(monkeypatch):
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", AsyncMock(
        side_effect=AssertionError("Offline research test attempted network I/O")))
    monkeypatch.setattr(httpx.Client, "send", lambda *a, **kw: pytest.fail("Unexpected sync HTTP"))


async def invoke(operation, client, **kwargs):
    if operation == "plan":
        return await generate_research_plan(kwargs.pop("topic", "Topic"), inference_client=client, **kwargs)
    if operation == "evaluate":
        return await evaluate_research_progress(
            topic=kwargs.pop("topic", "Topic"), questions=["What happened?"],
            sources=kwargs.pop("sources", [SOURCE]), round_number=3, max_rounds=3,
            inference_client=client, **kwargs,
        )
    return await synthesize_research(
        topic=kwargs.pop("topic", "Topic"), objective="Investigate",
        research_questions=["What happened?"], sources=kwargs.pop("sources", [SOURCE]),
        inference_client=client, **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OUTPUTS)
async def test_research_authenticated_http_contract(operation):
    requests = []
    def respond(request):
        requests.append(request)
        assert request.url == "https://core.test/api/v1/ai/generate"
        assert request.headers["X-API-Key"] == "test-only-research-key"
        payload = json.loads(request.content)
        assert payload["response_schema"] == MODELS[operation].model_json_schema()
        assert payload["temperature"] == (0.2 if operation == "plan" else 0)
        assert payload["timeout"] == (180 if operation == "synthesize" else 120)
        # The actual Core request contract accepts the complete unmodified prompt.
        from app.api.v1.ai import AIRequest
        AIRequest.model_validate(payload)
        if operation == "evaluate":
            assert '3 of 3' in payload["user_prompt"] and 'True' in payload["user_prompt"]
            assert 'choose "finish"' in payload["user_prompt"]
        return httpx.Response(200, json={"status": "success", "model": "actual-core-model",
                                        "result": json.dumps(OUTPUTS[operation])})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = ArgusCoreClient(base_url="https://core.test", api_key="test-only-research-key", http_client=http)
        result = await invoke(operation, client)
    assert result == MODELS[operation].model_validate(OUTPUTS[operation])
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OUTPUTS)
@pytest.mark.parametrize("status,expected", [(401, 502), (403, 502), (429, 429), (503, 503), (504, 504)])
async def test_research_core_failures_are_sanitized_without_retries(operation, status, expected):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers={"Retry-After": "60"}, text="private upstream diagnostics")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = ArgusCoreClient(base_url="https://core.test", api_key="test-only-research-key", http_client=http)
        with pytest.raises(CoreClientError) as error:
            await invoke(operation, client)
    assert error.value.status_code == expected
    assert error.value.retry_after == ("60" if status in {429, 503} else None)
    assert "private" not in str(error.value) and "test-only-research-key" not in str(error.value)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OUTPUTS)
@pytest.mark.parametrize("kind", ["timeout", "malformed_envelope", "invalid_output", "invalid_json"])
async def test_research_timeout_and_validation(operation, kind):
    requests = []
    def respond(request):
        requests.append(request)
        if kind == "timeout":
            raise httpx.ReadTimeout("private transport diagnostic", request=request)
        if kind == "malformed_envelope":
            return httpx.Response(200, json={"status": "success", "result": "{}"})
        content = "not JSON" if kind == "invalid_json" else '{"invalid":true}'
        return httpx.Response(200, json={"status": "success", "model": "actual-core-model", "result": content})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = ArgusCoreClient(base_url="https://core.test", api_key="test-only-research-key", http_client=http)
        with pytest.raises(CoreClientError if kind in {"timeout", "malformed_envelope"} else ValidationError) as error:
            await invoke(operation, client)
    if isinstance(error.value, CoreClientError):
        assert error.value.status_code == (504 if kind == "timeout" else 502)
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OUTPUTS)
async def test_unconfigured_research_does_not_fall_back(operation, monkeypatch):
    from app import global_chess_config
    monkeypatch.setattr(global_chess_config.global_chess_settings, "core_base_url", None)
    with pytest.raises(CoreClientError) as error:
        await invoke(operation, None)
    assert error.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OUTPUTS)
async def test_oversized_prompts_fail_before_inference(operation):
    client = AsyncMock()
    kwargs = {"topic": "x" * 30000}
    # The existing 25 x 1500 snippet allowance alone exceeds Core's limit.
    if operation == "synthesize":
        kwargs = {"sources": [dict(SOURCE, snippet="x" * 1500) for _ in range(25)]}
    with pytest.raises(CoreClientError, match="30000") as error:
        await invoke(operation, client, **kwargs)
    assert error.value.status_code == 422
    client.generate_structured.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("system_size,user_size,valid", [(10000, 30000, True), (10001, 1, False), (1, 30001, False)])
async def test_prompt_limits_match_core_contract(system_size, user_size, valid):
    from app.api.v1.ai import AIRequest
    payload = dict(system_prompt="s" * system_size, user_prompt="u" * user_size,
                   response_schema=ResearchPlan.model_json_schema(), timeout=120, temperature=0.2)
    client = AsyncMock()
    client.generate_structured.return_value = InferenceResult(json.dumps(PLAN), "actual-core-model")
    if valid:
        AIRequest.model_validate(payload)
        await generate_research_output(**{k: v for k, v in payload.items() if k != "response_schema"},
                                       response_model=ResearchPlan, inference_client=client)
        client.generate_structured.assert_awaited_once()
    else:
        with pytest.raises(ValidationError):
            AIRequest.model_validate(payload)
        with pytest.raises(CoreClientError):
            await generate_research_output(**{k: v for k, v in payload.items() if k != "response_schema"},
                                           response_model=ResearchPlan, inference_client=client)
        client.generate_structured.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("timestamp", [datetime(2026, 11, 6, 12, tzinfo=timezone.utc), date(2026, 11, 6)])
async def test_datetime_serialization_and_existing_source_caps(timestamp):
    client = AsyncMock()
    client.generate_structured.return_value = InferenceResult(json.dumps(SYNTHESIS), "actual-core-model")
    # Keep 25 sources below the Core limit while checking the existing caps.
    sources = [dict(SOURCE, published_at=timestamp, snippet="x" * (1501 if i == 0 else 1),
                    title=f"source-{i}") for i in range(26)]
    await invoke("synthesize", client, sources=sources)
    prompt = client.generate_structured.call_args.kwargs["user_prompt"]
    data = json.loads(prompt.split("COLLECTED SOURCES:", 1)[1].split("Produce a structured", 1)[0])
    assert len(data) == 25 and len(data[0]["snippet"]) == 1500
    assert data[0]["published_at"] == timestamp.isoformat()
    assert sources[0]["published_at"] == timestamp  # caller's data remains unchanged


@pytest.mark.asyncio
@pytest.mark.parametrize("field,item", [
    ("timeline", {"event": "Claim", "source_urls": ["https://invented.test"]}),
    ("key_actors", {"name": "Actor", "role": "Role", "source_urls": ["https://invented.test"]}),
    ("conflicting_accounts", {"issue": "Issue", "accounts": [], "source_urls": ["https://invented.test"]}),
])
async def test_synthesis_rejects_invented_citations_across_sections(field, item):
    client = AsyncMock()
    client.generate_structured.return_value = InferenceResult(
        json.dumps(dict(SYNTHESIS, **{field: [item]})), "actual-core-model")
    with pytest.raises(ValueError, match="referenced URLs"):
        await invoke("synthesize", client)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", OUTPUTS)
@pytest.mark.parametrize("mode", ["valid", "invalid_key", "missing_scope"])
async def test_research_with_real_core_router_and_auth(operation, mode, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.api.v1 import ai
    from app.api.v1.router import api_router
    from app.core.models import CoreBase, ApplicationDB, APIKeyDB
    from app.core.rate_limit import require_rate_limit
    from app.database import get_db
    from app.factory import create_app
    from app.services.api_key_service import generate_api_key
    from app.services.rate_limiter import GENERAL_API_LIMIT, AI_GENERATION_LIMIT

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    CoreBase.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    key = generate_api_key(environment="test")
    with sessions() as db:
        application = ApplicationDB(name="Research contract", slug="research-contract")
        db.add(application)
        db.flush()
        db.add(APIKeyDB(application_id=application.id, name="Offline key", key_prefix=key.key_prefix,
                        key_hash=key.key_hash, scopes=[] if mode == "missing_scope" else ["ai:generate"]))
        db.commit()
    app = create_app(routers=(api_router,))
    def session():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = session
    for bucket, limit in (("general", GENERAL_API_LIMIT), ("ai_generation", AI_GENERATION_LIMIT)):
        app.dependency_overrides[require_rate_limit(bucket=bucket, rate_limit=limit)] = lambda: None
    backend = AsyncMock(return_value={"model": "actual-core-model", "message": {"content": json.dumps(OUTPUTS[operation])}})
    monkeypatch.setattr(ai.ollama_client, "generate_structured", backend)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
            client = ArgusCoreClient(base_url="http://core.test", api_key=(
                "invalid" if mode == "invalid_key" else key.plaintext_key), http_client=http)
            if mode == "valid":
                result = await invoke(operation, client)
                assert result == MODELS[operation].model_validate(OUTPUTS[operation])
                backend.assert_awaited_once()
            else:
                with pytest.raises(CoreClientError) as error:
                    await invoke(operation, client)
                assert error.value.status_code == 502
                backend.assert_not_called()
    finally:
        engine.dispose()
