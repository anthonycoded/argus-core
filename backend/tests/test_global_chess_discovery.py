import copy
import feedparser
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpcore
import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.clients.argus_core_client import ArgusCoreClient, CoreClientError, RSSDiscoveryError, get_core_discovery_client
from app.clients.discovery_contracts import CoreDiscoveryResult
from app.services import research_service

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", AsyncMock(
        side_effect=AssertionError("Unexpected live HTTP")))
    monkeypatch.setattr(feedparser, "parse", Mock(side_effect=AssertionError("Unexpected live RSS")))
    monkeypatch.setattr(research_service, "discover_gdelt_sources", AsyncMock(side_effect=AssertionError("Unexpected GDELT")))


@pytest.fixture
def payload():
    return json.loads((FIXTURES / "core_google_news.json").read_text())


def row(payload, suffix, publisher="Example News", title="A supplied report"):
    candidate = copy.deepcopy(payload["candidates"][0])
    candidate.update(title=title, publisher=publisher, url=f"https://example.com/{suffix}",
                     canonical_url=f"https://example.com/{suffix}", domain="example.com")
    candidate["provenance"]["original_url"] = candidate["url"]
    return candidate


def rss_source(suffix="rss"):
    return {"title": "RSS report", "url": f"https://example.com/{suffix}", "publisher": "Shared",
            "published_at": None, "snippet": "Supplied RSS evidence", "source_type": "rss", "_relevance": 1.0}


@pytest.mark.asyncio
async def test_rss_first_budgets_classification_reviews_dedup_and_diversity(monkeypatch, payload):
    events = []
    async def rss(query, limit, **kwargs):
        events.append(("rss", limit))
        return [rss_source()]
    monkeypatch.setattr(research_service, "discover_rss_sources", rss)
    payload["candidates"] = [row(payload, "rss", "Shared", "RSS duplicate"),
                              row(payload, "g1", "Shared", "Google relevant"),
                              row(payload, "g2", "Other", "Google other"),
                              row(payload, "partial", "Third", "Partial"),
                              row(payload, "irrelevant", "Fourth", "Irrelevant")]
    def respond(request):
        events.append(("core", json.loads(request.content)["limit"]))
        return httpx.Response(200, json=payload)
    async def classify(**kwargs):
        title = kwargs["title"]
        events.append(("classify", title))
        classification = "partially_relevant" if title == "Partial" else "irrelevant" if title == "Irrelevant" else "relevant"
        return SimpleNamespace(classification=classification, rationale="Supplied information")
    classifier = AsyncMock(side_effect=classify)
    monkeypatch.setattr(research_service, "classify_article_relevance", classifier)
    metrics = research_service.new_research_metrics(); reviews = []; processed = set()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = ArgusCoreClient(base_url="https://core.test", api_key="test-only-key", http_client=http)
        selected = await research_service.discover_sources("Topic", 3, metrics, reviews, processed, discovery_client=client)
        assert [item["url"] for item in selected] == ["https://example.com/rss", "https://example.com/g2", "https://example.com/g1"]
        assert events[:3] == [("rss", 9), ("classify", "RSS report"), ("core", 9)]
        assert reviews[0]["classification"] == "partially_relevant"
        assert reviews[0]["provider_metadata"]["provider"] == "google_news"
        assert metrics == {"candidates_found": 6, "rss_candidates": 1, "google_news_candidates": 5,
                           "gdelt_candidates": 0, "classified_relevant": 3, "classified_partially_relevant": 1,
                           "classified_irrelevant": 1, "classification_failed": 0}
        assert not any("_relevance" in item for item in selected)
        await research_service.discover_sources("Topic", 3, metrics, reviews, processed, discovery_client=client)
    assert classifier.await_count == 5 and len(reviews) == 1


@pytest.mark.asyncio
async def test_candidate_budget_stays_capped_at_75(monkeypatch, payload):
    rss = AsyncMock(return_value=[]); monkeypatch.setattr(research_service, "discover_rss_sources", rss)
    payload["candidates"] = []
    client = AsyncMock();client.discover_google_news.return_value = CoreDiscoveryResult.model_validate(payload)
    assert await research_service.discover_sources("Topic", 100, discovery_client=client) == []
    rss.assert_awaited_once_with("Topic", 75, discovery_client=client)
    client.discover_google_news.assert_awaited_once_with(query="Topic", limit=75)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_rss", [True, False])
async def test_partial_failure_is_an_error_with_retained_sources(monkeypatch, with_rss):
    monkeypatch.setattr(research_service, "discover_rss_sources", AsyncMock(return_value=[rss_source()] if with_rss else []))
    monkeypatch.setattr(research_service, "classify_article_relevance", AsyncMock(return_value=SimpleNamespace(classification="relevant")))
    client = AsyncMock();client.discover_google_news.side_effect = CoreClientError(429, "Discovery rate limit exceeded.", "60")
    with pytest.raises(research_service.ResearchDiscoveryError) as error:
        await research_service.discover_sources("Topic", discovery_client=client)
    assert len(error.value.partial_sources) == int(with_rss)
    assert error.value.status_code == 429 and error.value.retry_after == "60"
    client.discover_google_news.assert_awaited_once()


@pytest.fixture
def database():
    from app.database import Base
    from app.models import Topic
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    with sessions() as db:
        db.add(Topic(id=1, title="Research topic", url="https://example.com/topic", source="Example",
                     relevance_score=1, freshness_score=1, score=1, status="approved"))
        db.commit()
    yield sessions
    engine.dispose()


def application(sessions, discovery_client):
    from app.database import get_db
    from app.routes.research import router
    from app.global_chess_factory import create_app
    app = create_app(routers=(router,))
    def session():
        with sessions() as db: yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_core_discovery_client] = lambda: discovery_client
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("classification", ["relevant", "partially_relevant", "irrelevant"])
async def test_rss_snapshot_failure_partial_headers_and_persistence(database, monkeypatch, payload, classification):
    from app.models import ResearchSource, Topic
    payload["candidates"] = []
    source = rss_source();source["title"] = "Ukraine NATO tensions"
    client = AsyncMock()
    client.discover_rss.side_effect = [RSSDiscoveryError(CoreClientError(410, "RSS snapshot expired."), [source]), []]
    client.discover_google_news.return_value = CoreDiscoveryResult.model_validate(payload)
    monkeypatch.setattr(research_service, "classify_article_relevance", AsyncMock(return_value=SimpleNamespace(
        classification=classification, rationale="Supplied assessment")))
    with database() as db:
        db.get(Topic, 1).title = "Ukraine NATO tensions";db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(database, client)), base_url="http://gc.test") as http:
        response = await http.post("/topics/1/research")
    assert response.status_code == (410 if classification == "irrelevant" else 200)
    if classification != "irrelevant":
        assert response.headers["X-Research-Partial"] == "true"
        assert response.headers["X-Research-Discovery-Status"] == "410"
        assert response.json()["inserted"] == int(classification == "relevant")
        assert len(response.json()["review_queue"]) == int(classification == "partially_relevant")
    with database() as db:
        assert len(db.scalars(select(ResearchSource)).all()) == int(classification == "relevant")
        assert db.get(Topic, 1).status == "approved"
    assert client.discover_rss.await_count == 2


@pytest.mark.asyncio
async def test_agent_rss_failure_keeps_evidence_and_stops_inference(monkeypatch, payload):
    from app.agents.research_agent import run_research_agent
    from app.clients.argus_core_client import InferenceResult
    payload["candidates"] = []
    source = rss_source();source["title"] = "Ukraine NATO tensions"
    discovery = AsyncMock()
    discovery.discover_rss.side_effect = [RSSDiscoveryError(CoreClientError(429, "RSS throttled.", "60"), [source]), []]
    discovery.discover_google_news.return_value = CoreDiscoveryResult.model_validate(payload)
    inference = AsyncMock()
    inference.generate_structured.return_value = InferenceResult(json.dumps({
        "objective":"Investigate", "research_questions":["Ukraine NATO tensions"]}), "actual-core-model")
    monkeypatch.setattr(research_service, "classify_article_relevance", AsyncMock(return_value=SimpleNamespace(classification="relevant")))
    result = await run_research_agent("Ukraine NATO tensions", inference_client=inference, discovery_client=discovery)
    assert result.status == "incomplete" and result.sources_discovered == 1 and result.synthesis is None
    assert any("RSS throttled" in limitation and "Retry-After: 60" in limitation for limitation in result.limitations)
    assert inference.generate_structured.await_count == 1 and discovery.discover_rss.await_count == 2


@pytest.mark.asyncio
async def test_research_route_persists_only_existing_fields_and_updates_as_before(database, monkeypatch, payload):
    from app.models import ResearchSource, Topic
    monkeypatch.setattr(research_service, "discover_rss_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(research_service, "classify_article_relevance", AsyncMock(return_value=SimpleNamespace(classification="relevant")))
    client = AsyncMock(); client.discover_google_news.return_value = CoreDiscoveryResult.model_validate(payload)
    app = application(database, client)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gc.test") as http:
        first = await http.post("/topics/1/research")
        assert first.status_code == 200 and first.json()["inserted"] == 1
        assert set(first.json()) == {"topic_id", "topic_title", "discovered", "inserted", "updated", "total_saved", "sources",
                                     "review_queue", "classification_coverage", *research_service.new_research_metrics()}
        assert set(first.json()["sources"][0]) == {"id", "title", "url", "publisher", "published_at", "source_type"}
        payload["candidates"][0]["title"] = "Updated title"
        payload["candidates"][0]["snippet"] = "Updated snippet"
        client.discover_google_news.return_value = CoreDiscoveryResult.model_validate(payload)
        second = await http.post("/topics/1/research")
        assert second.status_code == 200 and second.json()["updated"] == 1
    with database() as db:
        saved, = db.scalars(select(ResearchSource)).all()
        assert saved.title == "Updated title" and saved.snippet == "Only supplied source information."
        assert saved.source_type == "google_news" and db.get(Topic, 1).status == "approved"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_rss", [True, False])
@pytest.mark.parametrize("upstream,expected", [(401, 502), (403, 502), (429, 429), (503, 503), (504, 504)])
async def test_route_failure_and_partial_persistence(database, monkeypatch, with_rss, upstream, expected):
    from app.models import ResearchSource, Topic
    monkeypatch.setattr(research_service, "discover_rss_sources", AsyncMock(return_value=[rss_source()] if with_rss else []))
    monkeypatch.setattr(research_service, "classify_article_relevance", AsyncMock(return_value=SimpleNamespace(classification="relevant")))
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(upstream, headers={"Retry-After": "60"}, text="private diagnostics")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as core:
        app = application(database, ArgusCoreClient(base_url="https://core.test", api_key="test-only-key", http_client=core))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gc.test") as http:
            response = await http.post("/topics/1/research")
    assert response.status_code == (200 if with_rss else expected)
    assert "private" not in response.text and len(requests) == 1
    if with_rss:
        assert response.headers["X-Research-Partial"] == "true"
        assert response.headers["X-Research-Discovery-Status"] == str(expected)
    else:
        assert set(response.json()) == {"detail"}
    assert response.headers.get("Retry-After") == ("60" if upstream in {429, 503} else None)
    with database() as db:
        assert len(db.scalars(select(ResearchSource)).all()) == int(with_rss)
        assert db.get(Topic, 1).status == "approved"


@pytest.mark.asyncio
async def test_topic_approval_and_missing_topic_still_prevent_discovery(database):
    from app.models import Topic
    client = AsyncMock(); app = application(database, client)
    with database() as db:
        db.get(Topic, 1).status = "candidate";db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gc.test") as http:
        assert (await http.post("/topics/1/research")).status_code == 409
        assert (await http.post("/topics/999/research")).status_code == 404
    client.discover_google_news.assert_not_called()


@pytest.mark.asyncio
async def test_rss_review_queue_survives_core_failure_without_inserting_sources(database, monkeypatch):
    from app.models import ResearchSource
    monkeypatch.setattr(research_service, "discover_rss_sources", AsyncMock(return_value=[rss_source()]))
    monkeypatch.setattr(research_service, "classify_article_relevance", AsyncMock(return_value=SimpleNamespace(
        classification="partially_relevant", rationale="Needs editorial review")))
    client = AsyncMock();client.discover_google_news.side_effect = CoreClientError(429, "Discovery rate limit exceeded.", "60")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(database, client)), base_url="http://gc.test") as http:
        response = await http.post("/topics/1/research")
    assert response.status_code == 200 and response.headers["X-Research-Partial"] == "true"
    assert response.json()["review_queue"][0]["rationale"] == "Needs editorial review"
    assert response.json()["inserted"] == 0 and response.headers["Retry-After"] == "60"
    with database() as db:
        assert db.scalars(select(ResearchSource)).all() == []


@pytest.mark.asyncio
async def test_failed_core_request_leaves_existing_records_unchanged(database, monkeypatch):
    from app.models import ResearchSource
    with database() as db:
        db.add(ResearchSource(topic_id=1, title="Previously saved", url="https://example.com/existing",
                              snippet="Existing evidence", publisher="Example", source_type="google_news"))
        db.commit()
    monkeypatch.setattr(research_service, "discover_rss_sources", AsyncMock(return_value=[]))
    client = AsyncMock();client.discover_google_news.side_effect = CoreClientError(503, "Discovery service unavailable.", "30")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(database, client)), base_url="http://gc.test") as http:
        response = await http.post("/topics/1/research")
    assert response.status_code == 503 and response.headers["Retry-After"] == "30"
    with database() as db:
        saved, = db.scalars(select(ResearchSource)).all()
        assert saved.title == "Previously saved" and saved.snippet == "Existing evidence"


@pytest.mark.asyncio
async def test_research_write_failure_still_rolls_back(database, monkeypatch, payload):
    from sqlalchemy.orm import Session
    from app.models import ResearchSource
    monkeypatch.setattr(research_service, "discover_rss_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(research_service, "classify_article_relevance", AsyncMock(return_value=SimpleNamespace(classification="relevant")))
    client = AsyncMock();client.discover_google_news.return_value = CoreDiscoveryResult.model_validate(payload)
    monkeypatch.setattr(Session, "commit", Mock(side_effect=RuntimeError("isolated write failure")))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application(database, client)), base_url="http://gc.test") as http:
        response = await http.post("/topics/1/research")
    assert response.status_code == 500 and response.json()["detail"] == "Failed to save research sources"
    with database() as db:
        assert db.scalars(select(ResearchSource)).all() == []


@pytest.mark.asyncio
async def test_agent_keeps_partial_evidence_without_more_core_requests(monkeypatch):
    from app.agents import research_agent
    from app.clients.argus_core_client import InferenceResult
    inference = AsyncMock()
    inference.generate_structured.return_value = InferenceResult(
        json.dumps({"objective": "Investigate", "research_questions": ["What happened?"]}), "actual-core-model")
    monkeypatch.setattr(research_service, "discover_rss_sources", AsyncMock(return_value=[rss_source()]))
    monkeypatch.setattr(research_service, "classify_article_relevance", AsyncMock(return_value=SimpleNamespace(classification="relevant")))
    discovery = AsyncMock();discovery.discover_google_news.side_effect = CoreClientError(429, "Discovery rate limit exceeded.", "60")
    result = await research_agent.run_research_agent("Topic", inference_client=inference, discovery_client=discovery)
    assert result.status == "incomplete" and result.sources_discovered == 1 and result.synthesis is None
    assert result.sources[0]["url"] == "https://example.com/rss"
    assert any("Retry-After: 60" in item for item in result.limitations)
    assert result.rounds_completed == 1
    inference.generate_structured.assert_awaited_once()
    discovery.discover_google_news.assert_awaited_once()


@pytest.mark.asyncio
async def test_later_round_failure_keeps_previous_google_evidence(monkeypatch, payload):
    from app.agents import research_agent
    from app.clients.argus_core_client import InferenceResult
    monkeypatch.setattr(research_service, "discover_rss_sources", AsyncMock(return_value=[]))
    monkeypatch.setattr(research_service, "classify_article_relevance", AsyncMock(return_value=SimpleNamespace(classification="relevant")))
    inference = AsyncMock()
    inference.generate_structured.side_effect = [
        InferenceResult(json.dumps({"objective": "Investigate", "research_questions": ["What happened?"]}), "core-model"),
        InferenceResult(json.dumps({"decision": "continue", "reasoning": "More evidence", "missing_information": ["Why?"]}), "core-model"),
    ]
    discovery = AsyncMock()
    discovery.discover_google_news.side_effect = [CoreDiscoveryResult.model_validate(payload),
                                                 CoreClientError(503, "Discovery service unavailable.", "60")]
    result = await research_agent.run_research_agent("Topic", inference_client=inference, discovery_client=discovery)
    assert result.status == "incomplete" and result.rounds_completed == 2 and result.sources_discovered == 1
    assert result.sources[0]["provider_metadata"]["provenance"]["provider_item_id"] == "fixture"
    assert any("Retry-After: 60" in limitation for limitation in result.limitations)
    assert inference.generate_structured.await_count == 2 and result.synthesis is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["success", "empty", "invalid_key", "missing_scope", "throttle", "timeout", "malformed"])
async def test_actual_core_router_contract_and_authentication(monkeypatch, mode):
    from app.api.v1.router import api_router
    from app.api.v1.discovery import get_discovery_provider, require_discovery_rate_limit
    from app.core.database import get_db
    from app.core.models import CoreBase, ApplicationDB, APIKeyDB
    from app.core.rate_limit import require_rate_limit
    from app.factory import create_app
    from app.services.api_key_service import generate_api_key
    from app.services.discovery.google_news import GoogleNewsProvider
    from app.services.discovery.contracts import DiscoveryError
    from app.services.rate_limiter import GENERAL_API_LIMIT

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    CoreBase.metadata.create_all(engine);sessions = sessionmaker(bind=engine)
    key = generate_api_key(environment="test")
    with sessions() as db:
        app_record = ApplicationDB(name="GC discovery contract", slug="gc-discovery")
        db.add(app_record);db.flush()
        db.add(APIKeyDB(application_id=app_record.id, name="Offline key", key_prefix=key.key_prefix,
                        key_hash=key.key_hash, scopes=["ai:generate"] if mode == "missing_scope" else ["discovery:read"]))
        db.commit()
    fetcher = AsyncMock()
    fetcher.fetch_google_news.return_value = (FIXTURES / ("empty.xml" if mode == "empty" else "google_news.xml")).read_bytes()
    if mode == "throttle": fetcher.fetch_google_news.side_effect = DiscoveryError("throttled", "60")
    elif mode == "timeout": fetcher.fetch_google_news.side_effect = DiscoveryError("timeout")
    elif mode == "malformed": fetcher.fetch_google_news.return_value = b"not XML"
    app = create_app(routers=(api_router,))
    def session():
        with sessions() as db: yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_discovery_provider] = lambda: GoogleNewsProvider(fetcher)
    app.dependency_overrides[require_rate_limit(bucket="general", rate_limit=GENERAL_API_LIMIT)] = lambda: None
    app.dependency_overrides[require_discovery_rate_limit] = lambda: None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as http:
            client = ArgusCoreClient(base_url="http://core.test", api_key="invalid" if mode == "invalid_key" else key.plaintext_key, http_client=http)
            if mode in {"success", "empty"}:
                sources = await research_service.discover_google_news_sources("Topic", discovery_client=client)
                assert len(sources) == (2 if mode == "success" else 0)
                if sources:
                    assert sources[0]["published_at"].utcoffset().total_seconds() == 0
                    assert sources[0]["provider_metadata"]["provenance"]["provider_item_id"] == "fixture-one"
            else:
                with pytest.raises(CoreClientError) as error:
                    await research_service.discover_google_news_sources("Topic", discovery_client=client)
                assert error.value.status_code == (429 if mode == "throttle" else 504 if mode == "timeout" else 502)
                assert key.plaintext_key not in str(error.value)
                if mode in {"invalid_key", "missing_scope"}:fetcher.fetch_google_news.assert_not_called()
    finally:
        engine.dispose()
