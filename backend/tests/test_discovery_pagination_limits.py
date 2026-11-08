"""Real Redis quotas, in-process authenticated API, mocked news providers only."""

import os
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import ConnectionError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.discovery import get_discovery_provider, get_rss_provider
from app.api.v1.router import api_router
from app.core import rate_limit
from app.core.database import get_db
from app.core.models import APIKeyDB, ApplicationDB, CoreBase
from app.core.models.audit_log import AuditLogDB
from app.factory import create_app
from app.services import rate_limiter
from app.services.api_key_service import generate_api_key
from app.services.discovery.contracts import DiscoveryError, DiscoveryProviderResult
from app.services.discovery.rss import RSSProvider
from app.services.discovery.rss_fetch import FetchedRSS
from app.services.discovery.rss_store import RedisSnapshotStore


@pytest_asyncio.fixture
async def context(monkeypatch):
    url = os.getenv("ARGUS_DISCOVERY_TEST_REDIS_URL")
    expected_id = os.getenv("ARGUS_DISCOVERY_TEST_REDIS_RUN_ID")
    if not url or not expected_id:
        pytest.skip("Requires a disposable Redis URL and independently verified server run ID.")
    parsed = urlsplit(url)
    assert parsed.scheme == "redis" and parsed.hostname == "127.0.0.1" and parsed.port not in {None, 6379}
    assert parsed.path == "/0" and not parsed.username and not parsed.password
    redis = Redis.from_url(url, decode_responses=True)
    assert (await redis.info("server"))["run_id"] == expected_id
    assert (await redis.config_get("save"))["save"] == ""
    assert (await redis.config_get("appendonly"))["appendonly"] == "no"
    assert await redis.dbsize() == 0
    monkeypatch.setattr(rate_limiter, "redis_client", redis)
    monkeypatch.setattr(rate_limit, "enforce_rate_limit", rate_limiter.enforce_rate_limit)
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", AsyncMock(side_effect=AssertionError("Live HTTP")))

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    CoreBase.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    identities = []
    with sessions() as db:
        applications = [ApplicationDB(name=f"Test {i}", slug=f"pagination-{i}") for i in range(2)]
        db.add_all(applications); db.flush()
        for owner, scopes in [(0, ["discovery:read"]), (0, ["discovery:read"]), (1, ["discovery:read"]), (0, ["sources:read"])]:
            key = generate_api_key(environment="test")
            record = APIKeyDB(application_id=applications[owner].id, name="Offline", key_prefix=key.key_prefix,
                              key_hash=key.key_hash, scopes=scopes)
            db.add(record); db.flush()
            identities.append(SimpleNamespace(application_id=record.application_id, api_key_id=record.id, key=key.plaintext_key))
        db.commit()
    entries = "".join(f"<item><title>Ukraine NATO tensions {i}</title><link>https://example.com/{i}</link>"
                      "<pubDate>Fri, 06 Nov 2026 10:00:00 GMT</pubDate></item>" for i in range(500))
    fetcher = AsyncMock()
    fetcher.fetch.return_value = FetchedRSS(f"<rss version='2.0'><channel><title>Supplied feed</title>{entries}</channel></rss>".encode())
    rss = RSSProvider(RedisSnapshotStore(redis), fetcher)
    google = AsyncMock()
    google.discover.return_value = DiscoveryProviderResult()
    app = create_app(routers=(api_router,), audit_session_factory=sessions)
    def session():
        with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_rss_provider] = lambda: rss
    app.dependency_overrides[get_discovery_provider] = lambda: google
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://core.test") as http:
            yield SimpleNamespace(redis=redis, sessions=sessions, http=http, identities=identities,
                                  fetcher=fetcher, rss=rss, google=google)
    finally:
        # Only the independently verified disposable instance is touched.
        assert (await redis.info("server"))["run_id"] == expected_id
        keys = [key async for key in redis.scan_iter()]
        assert all(key.startswith(("argus:rate_limit:", "argus:discovery:rss:v1:")) for key in keys)
        if keys:
            await redis.delete(*keys)
        assert await redis.dbsize() == 0
        await redis.aclose()
        engine.dispose()


async def post(context, *, cursor=None, identity=0, feed_id="bbc-world", **extra):
    payload = dict(provider="rss", feed_id=feed_id, limit=75, **extra)
    if cursor is not None:
        payload["cursor"] = cursor
    return await context.http.post("/api/v1/discovery", headers={"X-API-Key": context.identities[identity].key}, json=payload)


async def count(context, bucket, identity=0, kind="application"):
    owner = context.identities[identity]
    identifier = owner.application_id if kind == "application" else owner.api_key_id
    return await context.redis.zcard(f"argus:rate_limit:{kind}:{identifier}:{bucket}")


async def charge(context, bucket, limit, times, identity=0):
    owner = context.identities[identity]
    for _ in range(times):
        assert (await rate_limiter.enforce_rate_limit(application_id=owner.application_id, api_key_id=owner.api_key_id,
                                                     bucket=bucket, rate_limit=limit)).allowed


def audits(context):
    with context.sessions() as db:
        return db.scalars(select(AuditLogDB).order_by(AuditLogDB.created_at)).all()


@pytest.mark.asyncio
async def test_two_full_snapshots_plus_google_fit_and_every_page_is_audited(context):
    for feed_id in ("bbc-world", "npr-politics"):
        sources = []
        cursor = None
        seen_cursors = set()
        snapshot_id = None
        for page_number in range(7):
            response = await post(context, feed_id=feed_id, cursor=cursor)
            assert response.status_code == 200
            page = response.json()
            assert page["feed_id"] == feed_id and page["status"] == "success"
            assert page["offset"] == len(sources) and page["total_entries"] == 500
            assert page["entries_in_page"] == len(page["candidates"]) == min(75, 500 - len(sources))
            snapshot_id = snapshot_id or page["snapshot_id"]
            assert page["snapshot_id"] == snapshot_id
            sources.extend(page["candidates"])
            if not page["has_more"]:
                assert page_number == 6 and page["next_cursor"] is None
                break
            cursor = page["next_cursor"]
            assert cursor and cursor not in seen_cursors
            seen_cursors.add(cursor)
        assert len(sources) == 500
        assert [s["provenance"]["entry_index"] for s in sources] == list(range(500))
    response = await context.http.post("/api/v1/discovery",
        headers={"X-API-Key": context.identities[0].key},
        json={"provider": "google_news", "query": "Ukraine NATO tensions", "limit": 10})
    assert response.status_code == 200 and response.json()["candidates"] == []
    for kind in ("application", "key"):
        assert await count(context, "general", kind=kind) == 15
        assert await count(context, "discovery", kind=kind) == 3
        assert await count(context, "rss_continuation", kind=kind) == 12
    assert context.fetcher.fetch.await_count == 2
    context.google.discover.assert_awaited_once()
    rows = audits(context)
    assert len(rows) == 15 and all(row.status_code == 200 for row in rows)
    assert all(row.application_id == context.identities[0].application_id and row.api_key_id == context.identities[0].api_key_id for row in rows)


@pytest.mark.asyncio
async def test_previous_shared_discovery_budget_would_block_request_eleven(context):
    owner = context.identities[0]
    decisions = [await rate_limiter.enforce_rate_limit(application_id=owner.application_id, api_key_id=owner.api_key_id,
        bucket="discovery", rate_limit=rate_limiter.DISCOVERY_LIMIT) for _ in range(15)]
    assert [d.allowed for d in decisions] == [True]*10 + [False]*5
    assert 1 <= decisions[10].retry_after <= 60


@pytest.mark.asyncio
async def test_exhausted_creation_quota_does_not_block_continuation_or_allow_new_fetch(context):
    first = await post(context)
    assert first.status_code == 200
    await charge(context, "discovery", rate_limiter.DISCOVERY_LIMIT, 9)
    following = await post(context, cursor=first.json()["next_cursor"])
    assert following.status_code == 200 and following.headers["X-RateLimit-Limit"] == "60"
    for response in [await post(context), await post(context, feed_id="npr-politics"),
                     await context.http.post("/api/v1/discovery", headers={"X-API-Key":context.identities[0].key}, json={"query":"Supplied query"})]:
        assert response.status_code == 429 and response.headers["X-RateLimit-Limit"] == "10"
        assert 1 <= int(response.headers["Retry-After"]) <= 60
    assert await count(context, "discovery") == 10 and await count(context, "rss_continuation") == 1
    context.fetcher.fetch.assert_awaited_once()
    context.google.discover.assert_not_awaited()
    assert [row.status_code for row in audits(context)] == [200, 200, 429, 429, 429]


@pytest.mark.asyncio
async def test_continuation_quota_shared_between_keys_and_separate_between_applications(context):
    first = await post(context)
    cursor = first.json()["next_cursor"]
    for _ in range(60):
        assert (await post(context, cursor=cursor)).status_code == 200
    blocked = await post(context, cursor=cursor, identity=1)
    assert blocked.status_code == 429 and blocked.headers["X-RateLimit-Limit"] == "60"
    assert 1 <= int(blocked.headers["Retry-After"]) <= 60
    assert await count(context, "rss_continuation", identity=1, kind="key") == 0
    # Another application has its own quota, but cannot use this application's cursor.
    foreign = await post(context, cursor=cursor, identity=2)
    assert foreign.status_code == 400 and await count(context, "rss_continuation", identity=2) == 1
    context.fetcher.fetch.assert_awaited_once()
    assert audits(context)[-2].api_key_id == context.identities[1].api_key_id


@pytest.mark.asyncio
async def test_general_quota_still_blocks_continuations(context):
    first = await post(context)
    await charge(context, "general", rate_limiter.GENERAL_API_LIMIT, 119)
    response = await post(context, cursor=first.json()["next_cursor"])
    assert response.status_code == 429 and response.headers["X-RateLimit-Limit"] == "120"
    assert 1 <= int(response.headers["Retry-After"]) <= 60
    assert await count(context, "rss_continuation") == 0
    context.fetcher.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_application_can_continue_with_another_authorized_key(context):
    first = await post(context)
    response = await post(context, cursor=first.json()["next_cursor"], identity=1)
    assert response.status_code == 200 and response.json()["offset"] == 75
    assert await count(context, "rss_continuation", identity=1, kind="key") == 1
    context.fetcher.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_bad_cross_app_wrong_size_and_expired_cursors_never_fetch(context):
    first = await post(context)
    cursor = first.json()["next_cursor"]
    for response in [await post(context, cursor="malformed"), await post(context, cursor=cursor, identity=2),
                     await post(context, cursor=cursor[:-8]+("A" if cursor[-8]!="A" else "B")+cursor[-7:]),
                     await context.http.post("/api/v1/discovery", headers={"X-API-Key":context.identities[0].key},
                                             json={"provider":"rss","feed_id":"bbc-world","limit":1,"cursor":cursor})]:
        assert response.status_code == 400
    # Simulate expiration/eviction exclusively inside the verified disposable Redis.
    await context.redis.delete(context.rss.store.key("bbc-world", "snapshot"))
    response = await post(context, cursor=cursor)
    assert response.status_code == 410 and response.json()["status"] == "cursor_expired"
    assert await count(context, "discovery") == 1 and await count(context, "rss_continuation") == 4
    context.fetcher.fetch.assert_awaited_once()
    assert await context.redis.exists(context.rss.store.key("bbc-world", "lease")) == 0


@pytest.mark.asyncio
async def test_cursor_authentication_permissions_and_audit_remain_required(context):
    for headers, status in [({},401), ({"X-API-Key":"invalid"},401), ({"X-API-Key":context.identities[3].key},403)]:
        response = await context.http.post("/api/v1/discovery", headers=headers,
            json={"provider":"rss","feed_id":"bbc-world","cursor":"malformed"})
        assert response.status_code == status
    assert await context.redis.dbsize() == 0
    context.fetcher.fetch.assert_not_awaited()
    rows = audits(context)
    assert [row.status_code for row in rows] == [401,401,403]
    assert rows[-1].api_key_id == context.identities[3].api_key_id


@pytest.mark.asyncio
async def test_provider_throttling_cooldown_unchanged(context):
    context.fetcher.fetch.side_effect = DiscoveryError("throttled", "17")
    first, second = await post(context), await post(context)
    assert first.status_code == second.status_code == 429
    assert first.headers["Retry-After"] == "17" and 1 <= int(second.headers["Retry-After"]) <= 17
    assert first.json()["status"] == second.json()["status"] == "throttled"
    context.fetcher.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_continuation_redis_outage_fails_closed(context, monkeypatch):
    first = await post(context)
    # General succeeds, then the actual continuation Redis command fails.
    real_eval = context.redis.eval
    async def fail_continuation(script, numkeys, *args):
        if any(isinstance(arg,str) and arg.endswith(":rss_continuation") for arg in args[:numkeys]):
            raise ConnectionError("Isolated failure")
        return await real_eval(script, numkeys, *args)
    monkeypatch.setattr(context.redis, "eval", fail_continuation)
    response = await post(context, cursor=first.json()["next_cursor"])
    assert response.status_code == 503 and response.headers["Retry-After"] == "5"
    assert "Isolated failure" not in response.text
    context.fetcher.fetch.assert_awaited_once()
    assert audits(context)[-1].status_code == 503
