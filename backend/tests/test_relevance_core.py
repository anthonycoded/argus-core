"""Offline relevance transport, coverage, workload and persistence regressions."""

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import anyio
import httpcore
import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agents import research_agent
from app.clients.argus_core_client import ArgusCoreClient, CoreClientError, InferenceResult, get_core_discovery_client, get_core_inference_client
from app.clients import classification_contracts as contracts
from app.clients.classification_contracts import ClassificationCoverage, CoverageSource, UnclassifiedCandidate
from app.database import Base, get_db
from app.global_chess_config import GlobalChessSettings
from app.global_chess_factory import create_app
from app.models import ResearchSource, Topic
from app.routes.research import router
from app.services import ollama_service, research_service
from app.services.classification_coverage import ClassificationContext, ClassificationFailure

FROZEN = json.loads((Path(__file__).parent / "fixtures/relevance_classifier.json").read_text())
KEY = "offline-relevance-key"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", AsyncMock(side_effect=AssertionError("Unexpected live network")))
    direct = Mock(side_effect=AssertionError("Direct Ollama construction is prohibited"))
    monkeypatch.setattr("app.clients.global_chess_ollama_client.OllamaClient.__init__", direct)
    yield
    direct.assert_not_called()


def candidate(index=0, **changes):
    return {"title": f"Article {index}", "url": f"https://example.com/{index}", "publisher": f"Publisher {index}",
            "published_at": None, "snippet": f"Original description {index}", "source_type": "rss", **changes}


def envelope(label="relevant", **changes):
    return {"status": "success", "model": "actual-core-model", "result": json.dumps({"classification": label, "rationale": "Supplied information", **changes})}


def core(http):
    return ArgusCoreClient(base_url="https://core.test", api_key=KEY, http_client=http)


def discovery(monkeypatch, rss, google=None):
    first = AsyncMock(return_value=rss)
    second = AsyncMock(return_value=google or [])
    monkeypatch.setattr(research_service, "discover_rss_sources", first)
    monkeypatch.setattr(research_service, "discover_google_news_sources", second)
    return first, second


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["relevant", "partially_relevant", "irrelevant"])
@pytest.mark.parametrize("snippet", [None, "Original description"])
async def test_exact_editorial_wire_contract_and_actual_model(label, snippet):
    calls = []
    def respond(request):
        payload = json.loads(request.content); calls.append(payload)
        assert request.url == "https://core.test/api/v1/ai/generate"
        assert request.headers["X-API-Key"] == KEY
        assert payload == {"system_prompt": FROZEN["system_prompt"],
                           "user_prompt": FROZEN["user_template"].format(topic="Topic", title="Title", description=snippet or "No description available."),
                           "response_schema": FROZEN["response_schema"], "temperature": 0.0, "timeout": 60.0}
        return httpx.Response(200, json=envelope(label))
    diagnostics = {}
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await ollama_service.classify_article_relevance(topic="Topic", title="Title", snippet=snippet,
                                                               inference_client=core(http), diagnostics=diagnostics)
    assert result.classification == label and result.rationale == "Supplied information"
    assert diagnostics == {"model": "actual-core-model"} and len(calls) == 1
    assert not hasattr(ollama_service, "ollama_client")


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [
    {"classification": "relevant", "rationale": ""}, {"classification": "relevant", "rationale": "x" * 501},
    {"classification": "unknown", "rationale": "Reason"}, {"classification": "relevant"},
    "not-json", [],
])
async def test_output_validation_never_calls_failure_irrelevant(output):
    client = AsyncMock()
    client.generate_structured.return_value = InferenceResult(output if isinstance(output, str) else json.dumps(output), "actual")
    with pytest.raises(ClassificationFailure) as error:
        await ollama_service.classify_article_relevance(topic="Topic", title="Title", inference_client=client)
    assert error.value.reason == "invalid_output" and error.value.attempted and not error.value.systemic
    client.generate_structured.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected,reason", [
    (401, 502, "authentication_failed"), (403, 502, "permission_denied"), (429, 429, "rate_limited"),
    (502, 502, "provider_error"), (503, 503, "service_unavailable"), (504, 504, "timeout"),
])
@pytest.mark.parametrize("retry", ["30", "private-invalid-retry"])
async def test_core_failures_sanitized_no_retry(status, expected, reason, retry, caplog):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": retry}, text=KEY)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(ClassificationFailure) as error:
            await ollama_service.classify_article_relevance(topic="Topic", title="Title", inference_client=core(http))
    assert error.value.status_code == expected and error.value.reason == reason and error.value.systemic
    assert error.value.retry_after == ("30" if status in {429, 503} and retry == "30" else None)
    assert len(calls) == 1 and KEY not in str(error.value) + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["timeout", "envelope", "network"])
async def test_transport_and_envelope_failure_types(fault):
    calls = []
    def respond(request):
        calls.append(request)
        if fault == "timeout": raise httpx.ReadTimeout(KEY, request=request)
        if fault == "network": raise httpx.ConnectError(KEY, request=request)
        return httpx.Response(200, json={"status": "success", "model": "actual", "result": None})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(ClassificationFailure) as error:
            await ollama_service.classify_article_relevance(topic="Topic", title="Title", inference_client=core(http))
    assert error.value.reason == {"timeout": "timeout", "envelope": "invalid_response", "network": "request_failed"}[fault]
    assert error.value.systemic and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [0, 1])
async def test_complete_user_prompt_boundary_without_truncation(extra):
    scaffold = len(FROZEN["user_template"].format(topic="", title="T", description="D"))
    topic = "t" * (30000 - scaffold + extra)
    client = AsyncMock(); client.generate_structured.return_value = InferenceResult(json.dumps({"classification": "relevant", "rationale": "Reason"}), "actual")
    if extra:
        with pytest.raises(ClassificationFailure) as error:
            await ollama_service.classify_article_relevance(topic=topic, title="T", snippet="D", inference_client=client)
        assert error.value.reason == "prompt_too_large" and not error.value.attempted
        client.generate_structured.assert_not_called()
    else:
        await ollama_service.classify_article_relevance(topic=topic, title="T", snippet="D", inference_client=client)
        assert len(client.generate_structured.call_args.kwargs["user_prompt"]) == 30000


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [0, 1])
async def test_system_prompt_guard_with_original_text(monkeypatch, extra):
    monkeypatch.setattr(ollama_service, "SYSTEM_PROMPT_LIMIT", len(FROZEN["system_prompt"]) - extra)
    client = AsyncMock(); client.generate_structured.return_value = InferenceResult(json.dumps({"classification": "relevant", "rationale": "Reason"}), "actual")
    if extra:
        with pytest.raises(ClassificationFailure):
            await ollama_service.classify_article_relevance(topic="T", title="T", inference_client=client)
        client.generate_structured.assert_not_called()
    else:
        await ollama_service.classify_article_relevance(topic="T", title="T", inference_client=client)
        assert client.generate_structured.call_args.kwargs["system_prompt"] == FROZEN["system_prompt"]


@pytest.mark.asyncio
async def test_oversized_observation_retained_and_not_suppressed_in_later_explicit_operation(monkeypatch):
    source = candidate(snippet="x" * 65536)
    discovery(monkeypatch, [source])
    context = ClassificationContext(); processed = set(); client = AsyncMock(); reviews = []
    with pytest.raises(research_service.ResearchDiscoveryError) as error:
        await research_service.discover_sources("Topic", inference_client=client, classification_context=context,
                                               processed_urls=processed, review_queue=reviews)
    coverage = error.value.classification_coverage
    assert not coverage.complete and coverage.attempted == coverage.failed == 0 and coverage.not_attempted == 1
    assert coverage.unclassified[0].source.snippet == source["snippet"] and coverage.unclassified[0].reason == "prompt_too_large"
    assert not processed and not reviews and context.evaluated == 1
    client.generate_structured.assert_not_called()
    discovery(monkeypatch, [candidate()])
    client.generate_structured.return_value = InferenceResult(json.dumps({"classification": "relevant", "rationale": "Reason"}), "actual")
    assert len(await research_service.discover_sources("Topic", inference_client=client, processed_urls=processed)) == 1


@pytest.mark.asyncio
async def test_individual_failure_continues_preserving_order_reviews_and_duplicate_winner(monkeypatch):
    discovery(monkeypatch, [candidate(0), candidate(1)], [candidate(0, source_type="google_news"), candidate(2, source_type="google_news")])
    outputs = ["invalid", json.dumps({"classification": "partially_relevant", "rationale": "Context"}),
               json.dumps({"classification": "relevant", "rationale": "Direct event"})]
    client = AsyncMock(); client.generate_structured.side_effect = [InferenceResult(item, "actual") for item in outputs]
    context = ClassificationContext(); reviews = []; processed = set()
    with pytest.raises(research_service.ResearchDiscoveryError) as error:
        await research_service.discover_sources("Topic", inference_client=client, classification_context=context,
                                               review_queue=reviews, processed_urls=processed)
    assert [item["url"] for item in error.value.partial_sources] == [candidate(2)["url"]]
    assert [item["url"] for item in reviews] == [candidate(1)["url"]]
    assert candidate(0)["url"] not in processed and len(processed) == 2
    assert context.snapshot().failed == 1 and context.snapshot().classified == 2
    assert context.models == ["actual"] * 3
    prompts = [call.kwargs["user_prompt"] for call in client.generate_structured.call_args_list]
    assert all(f"Article {index}" in prompt for index, prompt in enumerate(prompts))


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429, 502, 503, 504])
async def test_systemic_failure_preserves_earlier_evidence_and_known_remaining(monkeypatch, status):
    _, google = discovery(monkeypatch, [candidate(i) for i in range(3)])
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=envelope()) if len(calls) == 1 else httpx.Response(status, headers={"Retry-After": "30"}, text=KEY)
    context = ClassificationContext()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(research_service.ResearchDiscoveryError) as error:
            await research_service.discover_sources("Topic", inference_client=core(http), classification_context=context)
    coverage = context.snapshot()
    assert [source["url"] for source in error.value.partial_sources] == [candidate(0)["url"]]
    assert (coverage.attempted, coverage.classified, coverage.failed, coverage.not_attempted) == (2, 1, 1, 1)
    assert [item.status for item in coverage.unclassified] == ["failed", "not_attempted"]
    assert len(calls) == 2 and KEY not in coverage.model_dump_json()
    google.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", ["candidates", "time"])
async def test_operator_bounds_retain_remaining_candidates(monkeypatch, bound):
    _, google = discovery(monkeypatch, [candidate(i) for i in range(3)])
    context = ClassificationContext(max_candidates=1 if bound == "candidates" else 150, max_seconds=1,
                                    clock=iter([0.0, 2.0]).__next__ if bound == "time" else time.monotonic)
    client = AsyncMock();client.generate_structured.return_value = InferenceResult(envelope()["result"], "actual")
    with pytest.raises(research_service.ResearchDiscoveryError) as error:
        await research_service.discover_sources("Topic", inference_client=client, classification_context=context)
    assert context.snapshot().stop_reason == ("candidate_limit" if bound == "candidates" else "workload_deadline")
    assert context.snapshot().not_attempted == 2 and len(error.value.partial_sources) == 1
    client.generate_structured.assert_awaited_once();google.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_active_deadline_or_cancellation_preserves_evidence_and_cleanup(monkeypatch, cancel):
    discovery(monkeypatch, [candidate(i) for i in range(3)])
    started, finished = asyncio.Event(), asyncio.Event()
    calls = []
    async def respond(request):
        calls.append(request)
        if len(calls) == 1: return httpx.Response(200, json=envelope())
        started.set()
        try: await asyncio.Event().wait()
        finally: finished.set()
    context = ClassificationContext()
    # Use a real timeout armed after the second operation has started.
    original_timeout = asyncio.timeout; deadlines = []
    def timeout(seconds):
        scope = original_timeout(None);deadlines.append(scope);return scope
    monkeypatch.setattr(research_service.asyncio, "timeout", timeout)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        task = asyncio.create_task(research_service.discover_sources("Topic", inference_client=core(http), classification_context=context))
        await started.wait()
        if cancel:task.cancel()
        else:deadlines[-1].reschedule(asyncio.get_running_loop().time())
        with pytest.raises(asyncio.CancelledError if cancel else research_service.ResearchDiscoveryError):await task
    assert finished.is_set() and len(calls) == 2
    assert context.snapshot().stop_reason == ("cancelled" if cancel else "workload_deadline")
    assert context.snapshot().failed == context.snapshot().not_attempted == 1
    assert [source["url"] for source in context.completed_sources] == [candidate(0)["url"]]


def test_public_coverage_allowlist_dates_and_full_description(monkeypatch):
    source = candidate(snippet="x" * 65536, published_at="2026-11-07T07:00:00-05:00", private_notes=KEY,
                       provider_metadata={"provider": "rss", "canonical_url": candidate()["url"], "feed_id": "bbc-world",
                                          "provenance": {"entry_index": 0, "original_url": candidate()["url"], "private": KEY}, "api_key": KEY})
    public = CoverageSource.from_candidate(source)
    row = UnclassifiedCandidate(status="not_attempted", reason="prompt_too_large", source=public)
    coverage = ClassificationCoverage(complete=False, not_attempted=1, unclassified=[row])
    assert public.snippet == source["snippet"] and public.published_at.isoformat() == "2026-11-07T12:00:00+00:00"
    assert public.provenance.entry_index == 0 and KEY not in coverage.model_dump_json()
    with pytest.raises(ValidationError):CoverageSource(**{**public.model_dump(), "private": KEY})
    monkeypatch.setattr(contracts, "MAX_COVERAGE_BYTES", 100)
    with pytest.raises(ValidationError, match="response bound"):
        ClassificationCoverage(complete=False, not_attempted=1, unclassified=[row])


def test_coverage_cardinality_and_worst_case_serialized_bound():
    # JSON escaping needs at most six bytes per input character. The generous
    # hard ceiling admits the existing full-description contracts without loss.
    source_chars = 1000 + 2048 + 255 + 64 + 65536 + 50 + 2048 + 2048 + 40 + 32
    assert contracts.MAX_OBSERVATIONS * (source_chars * 6 + 2048) < contracts.MAX_COVERAGE_BYTES
    row = UnclassifiedCandidate(status="not_attempted", reason="candidate_limit", source=CoverageSource.from_candidate(candidate()))
    with pytest.raises(ValidationError):
        ClassificationCoverage(complete=False, not_attempted=226, unclassified=[row] * 226)


@pytest.mark.parametrize("name,value", [("classification_max_candidates", 0), ("classification_max_candidates", 151),
                                       ("classification_max_candidates", True), ("classification_max_candidates", 1.5),
                                       ("classification_max_seconds", 0), ("classification_max_seconds", 601),
                                       ("classification_max_seconds", float("inf")), ("classification_max_seconds", True)])
def test_workload_configuration_fail_closed(name, value):
    with pytest.raises(ValidationError):GlobalChessSettings(_env_file=None, **{name: value})


@pytest.fixture
def database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False)
    with sessions() as db:
        db.add(Topic(id=1, title="Topic", url="https://example.com/topic", source="Fixture", status="approved", summary="Preserved",
                     relevance_score=10, freshness_score=20, score=13))
        db.commit()
    yield sessions
    engine.dispose()


def application(database, inference):
    app = create_app(routers=(router,))
    def session():
        with database() as db:yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_core_inference_client] = lambda: inference
    app.dependency_overrides[get_core_discovery_client] = lambda: AsyncMock()
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("with_evidence", [False, True])
async def test_api_error_or_partial_coverage_preserves_persistence(database, monkeypatch, with_evidence):
    discovery(monkeypatch, [candidate(i) for i in range(3)])
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=envelope()) if with_evidence and len(calls) == 1 else httpx.Response(429, headers={"Retry-After": "30"}, text=KEY)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        app = application(database, core(http))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as browser:
            response = await browser.post("/topics/1/research")
            missing = await browser.post("/topics/2/research")
    assert response.status_code == (200 if with_evidence else 429)
    assert response.headers["Retry-After"] == "30" and not response.json()["classification_coverage"]["complete"]
    assert response.json()["classification_coverage"]["failed"] == 1
    assert KEY not in response.text and missing.json() == {"detail": "Topic not found"}
    if with_evidence:assert response.headers["X-Research-Partial"] == "true"
    else:assert response.json()["detail"] == "AI inference rate limit exceeded."
    with database() as db:
        assert len(list(db.scalars(select(ResearchSource)))) == int(with_evidence)
        assert db.get(Topic, 1).summary == "Preserved" and db.get(Topic, 1).status == "approved"


@pytest.mark.asyncio
async def test_agent_shares_budget_across_rounds_and_stops_before_more_inference(monkeypatch):
    first, second = discovery(monkeypatch, [], [candidate(0, source_type="google_news")])
    calls = []
    def respond(request):
        properties = json.loads(request.content)["response_schema"]["properties"]
        if "objective" in properties:operation, result = "plan", {"objective": "Research", "research_questions": ["Why?"]}
        elif "decision" in properties:operation, result = "evaluate", {"decision": "continue", "reasoning": "Gap", "missing_information": ["Why?"]}
        else:
            operation = "classify";calls.append(operation);return httpx.Response(200, json=envelope())
        calls.append(operation)
        return httpx.Response(200, json={"status": "success", "model": "actual", "result": json.dumps(result)})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await research_agent.run_research_agent("Topic", inference_client=core(http), classification_context=ClassificationContext(max_candidates=1))
    assert result.status == "incomplete" and result.sources_discovered == 1 and result.synthesis is None
    assert calls == ["plan", "classify", "evaluate"] and first.await_count == second.await_count == 1
    assert result.classification_coverage.stop_reason == "candidate_limit" and result.classification_coverage.classified == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("first_label", ["relevant", "partially_relevant"])
async def test_anyio_scope_cancellation_preserves_completed_observations(monkeypatch, first_label):
    discovery(monkeypatch, [candidate(i) for i in range(3)])
    started, finished = asyncio.Event(), asyncio.Event()
    calls = []
    async def respond(request):
        calls.append(request)
        if len(calls) == 1:return httpx.Response(200, json=envelope(first_label))
        started.set()
        try:await asyncio.Event().wait()
        finally:finished.set()
    context = ClassificationContext()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        async with anyio.create_task_group() as tasks:
            async def run():
                await research_service.discover_sources("Topic", inference_client=core(http), classification_context=context)
            tasks.start_soon(run)
            await started.wait()
            tasks.cancel_scope.cancel()
    assert finished.is_set() and len(calls) == 2
    assert context.snapshot().stop_reason == "cancelled"
    assert len(context.completed_sources) == int(first_label == "relevant")
    assert len(context.review_queue) == int(first_label == "partially_relevant")
    assert (context.snapshot().failed, context.snapshot().not_attempted) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["valid", "invalid_key", "missing_scope"])
async def test_classifier_through_real_core_authentication_and_admission_contract(monkeypatch, mode):
    from app.api.v1 import ai
    from app.core.database import get_db as get_core_db
    from app.core.models import CoreBase, ApplicationDB, APIKeyDB
    from app.core.rate_limit import require_rate_limit
    from app.factory import create_app as create_core_app
    from app.services.api_key_service import generate_api_key
    from app.services.rate_limiter import GENERAL_API_LIMIT
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    CoreBase.metadata.create_all(engine);sessions = sessionmaker(bind=engine)
    key = generate_api_key(environment="test")
    with sessions() as db:
        app_record = ApplicationDB(name="Offline relevance", slug="offline-relevance");db.add(app_record);db.flush()
        db.add(APIKeyDB(application_id=app_record.id, name="Offline", key_prefix=key.key_prefix,
                        key_hash=key.key_hash, scopes=[] if mode == "missing_scope" else ["ai:generate"]))
        db.commit()
    app = create_core_app();app.include_router(ai.router, prefix="/api/v1")
    def session():
        with sessions() as db:yield db
    app.dependency_overrides[get_core_db] = session
    app.dependency_overrides[require_rate_limit(bucket="general", rate_limit=GENERAL_API_LIMIT)] = lambda: None
    app.dependency_overrides[ai.require_ai_rate_limit] = lambda: None
    admission = SimpleNamespace(acquire=AsyncMock(), release=AsyncMock())
    app.dependency_overrides[ai.get_ai_admission] = lambda: admission
    backend = AsyncMock(return_value={"model": "actual-core-model", "message": {"content": envelope()["result"]}})
    monkeypatch.setattr(ai.ollama_client, "generate_structured", backend)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
            client = ArgusCoreClient(base_url="http://core.test", api_key="invalid" if mode == "invalid_key" else key.plaintext_key, http_client=http)
            if mode == "valid":
                assert (await ollama_service.classify_article_relevance(topic="Topic", title="Title", inference_client=client)).classification == "relevant"
                backend.assert_awaited_once();admission.release.assert_awaited_once()
            else:
                with pytest.raises(ClassificationFailure) as error:
                    await ollama_service.classify_article_relevance(topic="Topic", title="Title", inference_client=client)
                assert error.value.reason == ("authentication_failed" if mode == "invalid_key" else "permission_denied")
                backend.assert_not_called();admission.acquire.assert_not_called()
    finally:engine.dispose()


@pytest.mark.asyncio
async def test_classification_authentication_failure_is_not_masked_by_prior_feed_failure(monkeypatch):
    first, second = discovery(monkeypatch, [])
    first.side_effect = research_service.ResearchDiscoveryError(CoreClientError(503, "RSS unavailable.", "5"), [candidate()])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(401))) as http:
        with pytest.raises(research_service.ResearchDiscoveryError) as error:
            await research_service.discover_sources("Topic", inference_client=core(http))
    assert error.value.status_code == 502 and error.value.detail == "Argus-Core authentication failed."
    assert error.value.classification_coverage.stop_reason == "authentication_failed"
    second.assert_not_called()
