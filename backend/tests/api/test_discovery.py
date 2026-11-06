from pathlib import Path
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.discovery import get_discovery_provider
from app.api.v1.router import api_router
from app.core.database import Base, get_db
from app.core.models import CoreBase, ApplicationDB, APIKeyDB, SourceRecordDB
from app.factory import create_app
from app.services.api_key_service import generate_api_key
from app.services.discovery.fetch import SecureDiscoveryFetcher
from app.services.discovery.google_news import GoogleNewsProvider
from app.services.rate_limiter import RateLimitDecision

FIXTURE = Path(__file__).parents[1] / "fixtures" / "discovery" / "google_news.xml"


class Body(httpx.AsyncByteStream):
    def __init__(self, content): self.content = content
    async def __aiter__(self): yield self.content
    async def aclose(self): pass


@pytest.fixture
def context(monkeypatch):
    from app import models  # Register Global Chess tables only in in-memory SQLite.
    from app.core import rate_limit
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", AsyncMock(
        side_effect=AssertionError("Unexpected live network")))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    CoreBase.metadata.create_all(engine)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    key = generate_api_key(environment="test")
    with sessions() as db:
        application = ApplicationDB(name="Discovery test", slug="discovery-test")
        db.add(application); db.flush()
        api_key = APIKeyDB(application_id=application.id, name="Offline key", key_prefix=key.key_prefix,
                          key_hash=key.key_hash, scopes=["discovery:read"])
        db.add(api_key); db.commit()
        app_id, key_id = application.id, api_key.id
    writes = []
    def forbid_discovery_writes(conn, cursor, statement, parameters, context, executemany):
        sql = statement.strip().lower()
        if sql.startswith(("insert", "update", "delete", "create", "drop", "alter")):
            assert sql.startswith("update core_api_keys set last_used_at="), "Discovery attempted a data/schema write"
            writes.append(sql)
    event.listen(engine, "before_cursor_execute", forbid_discovery_writes)
    requests = []
    state = {"status": 200, "body": FIXTURE.read_bytes(), "headers": {}}
    def respond(request):
        requests.append(request)
        assert "x-api-key" not in request.headers and "authorization" not in request.headers
        if "exception" in state:
            raise state["exception"]
        return httpx.Response(state["status"], headers={"content-type": "application/rss+xml", **state["headers"]},
                              stream=Body(state["body"]))
    provider = GoogleNewsProvider(SecureDiscoveryFetcher(transport=httpx.MockTransport(respond)))
    app = create_app(routers=(api_router,))
    def request_session():
        with sessions() as db: yield db
    app.dependency_overrides[get_db] = request_session
    app.dependency_overrides[get_discovery_provider] = lambda: provider
    enforcement = AsyncMock(return_value=RateLimitDecision(allowed=True, limit=10, remaining=9, retry_after=0))
    monkeypatch.setattr(rate_limit, "enforce_rate_limit", enforcement)
    yield {"app": app, "key": key.plaintext_key, "sessions": sessions, "requests": requests,
           "state": state, "enforce": enforcement, "provider": provider,
           "app_id": app_id, "key_id": key_id, "writes": writes, "engine": engine,
           "write_guard": forbid_discovery_writes}
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(SourceRecordDB)) == 0
        for table in Base.metadata.sorted_tables:
            assert db.scalar(select(func.count()).select_from(table)) == 0
    engine.dispose()


async def post(context, payload=None, key=True):
    headers = {"X-API-Key": context["key"] if key is True else key} if key else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=context["app"]), base_url="http://core.test") as client:
        return await client.post("/api/v1/discovery", headers=headers, json=payload or {"query": "research topic", "limit": 10})


@pytest.mark.asyncio
async def test_discovery_auth_http_contract_and_no_source_persistence(context):
    response = await post(context)
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "google_news" and body["status"] == "success"
    assert len(body["candidates"]) == 2 and body["duplicates"] == 1 and body["invalid_candidates"] == 1
    assert body["candidates"][0]["published_at"] == "2026-11-06T10:00:00Z"
    assert len(context["requests"]) == 1
    calls = context["enforce"].call_args_list
    assert [c.kwargs["bucket"] for c in calls] == ["general", "discovery"]
    assert all(c.kwargs["application_id"] == context["app_id"] and c.kwargs["api_key_id"] == context["key_id"] for c in calls)
    assert calls[1].kwargs["rate_limit"].requests == 10
    assert len(context["writes"]) == 1  # Existing API-key last_used_at bookkeeping.


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [None, "invalid-api-key"])
async def test_authentication_fails_before_provider(context, key):
    response = await post(context, key=key)
    assert response.status_code == 401 and context["requests"] == []
    context["enforce"].assert_not_awaited()


@pytest.mark.asyncio
async def test_scope_required(context):
    # Change only the isolated fixture credential, outside discovery write guard.
    event.remove(context["engine"], "before_cursor_execute", context["write_guard"])
    with context["sessions"]() as db:
        db.get(APIKeyDB, context["key_id"]).scopes = ["sources:read"]
        db.commit()
    event.listen(context["engine"], "before_cursor_execute", context["write_guard"])
    response = await post(context)
    assert response.status_code == 403 and context["requests"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bucket", ["general", "discovery"])
async def test_rate_limit_blocks_provider(context, bucket):
    async def enforce(**kwargs):
        return RateLimitDecision(allowed=kwargs["bucket"] != bucket, limit=10, remaining=0, retry_after=30)
    context["enforce"].side_effect = enforce
    response = await post(context)
    assert response.status_code == 429 and response.headers["Retry-After"] == "30"
    assert context["requests"] == []


@pytest.mark.asyncio
async def test_redis_failure_fails_closed(context):
    context["enforce"].side_effect = RedisConnectionError("isolated failure")
    response = await post(context)
    assert response.status_code == 503 and context["requests"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"query": ""}, {"query": "x", "limit": 0}, {"query": "x", "limit": 76},
    {"query": "x", "provider": "gdelt"}, {"query": "x", "fetch_url": "http://127.0.0.1/"}])
async def test_api_rejects_invalid_inputs_without_fetching(context, payload):
    response = await post(context, payload)
    assert response.status_code == 422 and context["requests"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,http_status,provider_status", [
    ("empty", 200, "success"), ("malformed", 502, "malformed_feed"),
    ("throttle", 429, "throttled"), ("upstream", 502, "upstream_error"),
    ("timeout", 504, "timeout"), ("unexpected", 502, "upstream_error"),
])
async def test_provider_outcomes_are_explicit_and_sanitized(context, mode, http_status, provider_status, caplog):
    if mode == "empty": context["state"]["body"] = b"<rss><channel/></rss>"
    elif mode == "malformed": context["state"]["body"] = b"not XML private provider diagnostics"
    elif mode in {"throttle", "upstream"}:
        context["state"].update(status=429 if mode == "throttle" else 503,
                                headers={"retry-after": "60"}, body=b"private provider diagnostics")
    elif mode == "timeout":
        context["state"]["exception"] = httpx.ReadTimeout("private provider diagnostics")
    else:
        context["provider"]._fetcher.fetch_google_news = AsyncMock(side_effect=RuntimeError("private provider diagnostics"))
    response = await post(context)
    assert response.status_code == http_status and response.json()["status"] == provider_status
    assert "private provider diagnostics" not in response.text + caplog.text
    assert context["key"] not in response.text + caplog.text
    if mode in {"throttle", "upstream"}:
        assert response.headers["Retry-After"] == "60"


def test_discovery_registered_once_and_contract_is_documented(context):
    from app.api.v1.discovery import router
    assert sum(route.path == "/discovery" for route in router.routes) == 1
    schema = context["app"].openapi()
    assert "/api/v1/discovery" in schema["paths"]
