import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import httpcore
import httpx
import pytest

from app.services.discovery import feed_registry, rss, rss_parser
from app.services.discovery.contracts import DiscoveryError
from app.services.discovery.feed_registry import APPROVED_FEEDS, ApprovedFeed
from app.services.discovery.rss import RSSProvider
from app.services.discovery.rss_contracts import RSSCandidate, RSSDiscoveryRequest, RSSFault
from app.services.discovery.rss_fetch import FetchedRSS
from app.services.discovery.rss_parser import parse_rss
from app.services.discovery.rss_store import StoredSnapshot

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"


class MemoryStore:
    """Test double implementing immutable serialized slots and expiring leases."""
    def __init__(self):
        self.now = 1000.0; self.data = {}; self.leases = {}; self.deferred = {}
    async def load(self, feed_id):
        raw = self.data.get(feed_id)
        if raw:
            snapshot = StoredSnapshot.model_validate_json(raw)
            if snapshot.expires_at.timestamp() > self.now: return snapshot
            self.data.pop(feed_id)
        return None
    async def acquire(self, feed_id, token):
        if feed_id in self.leases: return False
        self.leases[feed_id] = token;return True
    async def release(self, feed_id, token):
        if self.leases.get(feed_id) == token:self.leases.pop(feed_id)
    async def publish(self, snapshot, token):
        if self.leases.get(snapshot.feed_id) != token or snapshot.feed_id in self.data:raise RSSFault("busy")
        self.data[snapshot.feed_id] = snapshot.model_dump_json()
    async def cooldown(self, feed_id):return max(0, int(self.deferred.get(feed_id, 0) - self.now))
    async def defer(self, feed_id, seconds):self.deferred[feed_id] = self.now + seconds


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", AsyncMock(side_effect=AssertionError("Live HTTP")))


def provider(data=None):
    store = MemoryStore();fetcher = AsyncMock()
    fetcher.fetch.return_value = FetchedRSS(data if data is not None else (FIXTURES / "bbc_world.xml").read_bytes())
    return RSSProvider(store, fetcher, clock=lambda: store.now), store, fetcher


def request(**kwargs):return RSSDiscoveryRequest(provider="rss", feed_id="bbc-world", **kwargs)


@pytest.mark.asyncio
async def test_complete_pagination_preserves_order_duplicates_and_rejections():
    service, store, fetcher = provider()
    first = await service.discover(request(limit=2), "app-one")
    assert first.status == "incomplete" and first.total_entries == 5 and first.invalid_entries == 2
    assert first.duplicate_observations == 1 and first.has_more
    assert [x.provenance.entry_index for x in first.candidates] == [0, 1]
    assert first.candidates[0].url == first.candidates[1].url
    assert first.candidates[0].publisher == "BBC World"
    assert first.candidates[0].published_at == datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    assert first.candidates[0].provenance.original_url.endswith("?utm_source=rss")
    assert "Supplied summary" in first.candidates[0].snippet
    second = await service.discover(request(limit=2, cursor=first.next_cursor), "app-one")
    assert second.snapshot_id == first.snapshot_id and second.offset == 2 and second.entries_in_page == 2
    assert second.candidates[0].published_at is None and second.candidates[0].provenance.original_url == "/news/three"
    assert second.rejected_entries[0].entry_index == 3
    third = await service.discover(request(limit=2, cursor=second.next_cursor), "app-one")
    assert third.rejected_entries[0].entry_index == 4 and not third.has_more and third.next_cursor is None
    fetcher.fetch.assert_awaited_once()
    assert store.leases == {} and "secret" not in first.model_dump_json()


@pytest.mark.asyncio
async def test_live_snapshot_is_reused_and_caller_mutation_cannot_change_it():
    service, store, fetcher = provider()
    first = await service.discover(request(limit=2), "app")
    first.candidates[0].title = "Caller changed"
    fetcher.fetch.return_value = FetchedRSS(b"malformed new feed")
    repeated = await service.discover(request(limit=2), "app")
    assert repeated.candidates[0].title == "First supplied report" and repeated.snapshot_id == first.snapshot_id
    fetcher.fetch.assert_awaited_once()
    store.now += 301
    expired = await service.discover(request(limit=2, cursor=first.next_cursor), "app")
    assert expired.status == "cursor_expired"
    fetcher.fetch.assert_awaited_once()
    fetcher.fetch.return_value=FetchedRSS((FIXTURES/"bbc_world.xml").read_bytes())
    fresh=await service.discover(request(limit=2),"app")
    assert fresh.snapshot_id!=first.snapshot_id
    assert (await service.discover(request(limit=2,cursor=first.next_cursor),"app")).status=="cursor_expired"


@pytest.mark.parametrize("fault", ["tamper", "owner", "limit", "format", "other_feed"])
@pytest.mark.asyncio
async def test_invalid_cursors_never_fetch(fault):
    service, store, fetcher = provider()
    first = await service.discover(request(limit=2), "app")
    cursor = first.next_cursor;owner = "app";limit = 2
    if fault == "tamper":
        raw = bytearray(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)));raw[-1] ^= 1
        cursor = base64.urlsafe_b64encode(raw).decode()
    elif fault == "owner":owner = "other"
    elif fault == "limit":limit = 3
    elif fault == "format":cursor = "not a valid cursor"
    else:
        result = await service.discover(RSSDiscoveryRequest(provider="rss", feed_id="npr-politics", limit=2, cursor=cursor), owner)
        assert result.status in {"invalid_cursor", "cursor_expired"};return
    result = await service.discover(request(limit=limit, cursor=cursor), owner)
    assert result.status == "invalid_cursor"
    fetcher.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_disabled_ids_and_overrides(monkeypatch):
    service, store, fetcher = provider()
    assert (await service.discover(RSSDiscoveryRequest(provider="rss", feed_id="unknown"), "app")).status == "unknown_feed"
    monkeypatch.setattr(feed_registry, "APPROVED_FEEDS", {"bbc-world": ApprovedFeed("bbc-world", APPROVED_FEEDS["bbc-world"].url, "BBC", False)})
    assert (await service.discover(request(), "app")).status == "unknown_feed"
    fetcher.fetch.assert_not_called()
    from pydantic import ValidationError
    for extra in ({"url": "http://localhost/"}, {"query": "topic"}, {"headers": {}}, {"limit": 0}, {"limit": 76}):
        with pytest.raises(ValidationError):RSSDiscoveryRequest(provider="rss", feed_id="bbc-world", **extra)


@pytest.mark.asyncio
async def test_empty_feed_success_and_npr_date_warning():
    service, _, _ = provider(b"<rss><channel><title>Empty</title></channel></rss>")
    result = await service.discover(request(), "app")
    assert result.status == "success" and result.total_entries == 0 and not result.has_more
    service, _, _ = provider((FIXTURES / "npr_politics.xml").read_bytes())
    result = await service.discover(RSSDiscoveryRequest(provider="rss", feed_id="npr-politics"), "app")
    assert result.status == "incomplete" and all(c.publisher == "NPR Politics" for c in result.candidates)
    assert result.candidates[0].published_at == datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    assert result.candidates[1].provenance.warnings == ["invalid_publication_date"]


def test_atom_namespace_summary_and_timezone():
    data = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title><entry><title>Report</title><id>id</id><link href="https://example.com/report"/><updated>2026-11-06T10:00:00Z</updated><summary>Snippet</summary><author><name>Reporter</name></author></entry></feed>'
    _, format_name, rows, _ = parse_rss(data, APPROVED_FEEDS["bbc-world"])
    assert format_name == "atom" and rows[0].snippet == "Snippet"
    assert rows[0].provenance.reported_publisher == "Reporter"


@pytest.mark.parametrize("base,valid",[("https://example.com/articles/",True),("http://169.254.169.254/",False)])
def test_xml_base_resolves_metadata_without_fetching(base,valid):
    data=f'<rss xml:base="{base}"><channel><item><title>Report</title><link>one</link></item></channel></rss>'.encode()
    _,_,rows,_=parse_rss(data,APPROVED_FEEDS["bbc-world"])
    if valid:
        assert rows[0].url=="https://example.com/articles/one" and rows[0].provenance.original_url=="one"
    else:assert rows[0].reason=="invalid_url"


@pytest.mark.parametrize("data", [b"<rss>", b"not XML", b"<html/>", b'<!DOCTYPE rss [<!ENTITY x "expansion">]><rss><channel/></rss>',
    '<!DOCTYPE rss SYSTEM "file:///no-read"><rss><channel/></rss>'.encode("utf-16")])
@pytest.mark.asyncio
async def test_malformed_xml_no_snapshot(data):
    service, store, _ = provider(data)
    assert (await service.discover(request(), "app")).status == "malformed_feed"
    assert store.data == {} and store.leases == {}


@pytest.mark.parametrize("kind", ["depth", "nodes", "entries", "bytes", "time", "snapshot"])
@pytest.mark.asyncio
async def test_limits_fail_explicitly_without_partial_snapshot(kind, monkeypatch):
    data = b"<rss><channel/></rss>"
    if kind == "depth":data = b"<rss><channel>" + b"<x>" * 33 + b"</x>" * 33 + b"</channel></rss>"
    elif kind == "nodes":data = b"<rss><channel>" + b"<x/>" * 10001 + b"</channel></rss>"
    elif kind == "entries":data = b"<rss><channel>" + b"<item/>" * 501 + b"</channel></rss>"
    elif kind == "bytes":data = b"x" * (1024 * 1024 + 1)
    elif kind == "time":monkeypatch.setattr(rss_parser, "PARSER_SECONDS", 0)
    else:monkeypatch.setattr(rss, "MAX_SNAPSHOT_BYTES", 10)
    service, store, _ = provider(data)
    assert (await service.discover(request(), "app")).status == "parser_limit"
    assert store.data == {} and store.leases == {}


@pytest.mark.asyncio
async def test_field_limits_report_rejected_observation_no_truncation():
    data = ('<rss><channel><item><title>' + 'x' * 8193 + '</title><link>https://example.com/</link></item></channel></rss>').encode()
    service, _, _ = provider(data)
    result = await service.discover(request(), "app")
    assert result.status == "incomplete" and result.rejected_entries[0].reason == "field_limit"


@pytest.mark.asyncio
async def test_shared_feed_lease_prevents_concurrent_fetch_and_throttle_cooldown():
    service, store, fetcher = provider()
    other = RSSProvider(store, fetcher, clock=lambda:store.now)
    entered = anyio.Event();release = anyio.Event();outputs = []
    async def wait(feed):
        entered.set();await release.wait();return FetchedRSS(b"<rss><channel/></rss>")
    fetcher.fetch.side_effect = wait
    async def first():outputs.append(await service.discover(request(), "app"))
    async with anyio.create_task_group() as group:
        group.start_soon(first);await entered.wait()
        assert (await other.discover(request(), "other")).status == "busy"
        release.set()
    assert outputs[0].status == "success" and fetcher.fetch.await_count == 1
    store.data.clear();fetcher.fetch.side_effect = DiscoveryError("throttled", "120")
    assert (await service.discover(request(), "app")).status == "throttled"
    assert (await other.discover(request(), "app")).retry_after == "120"
    assert fetcher.fetch.await_count == 2


@pytest.mark.asyncio
async def test_storage_failure_fails_closed():
    service, _, fetcher = provider()
    service.store = AsyncMock();service.store.load.side_effect = RSSFault("storage_unavailable")
    assert (await service.discover(request(), "app")).status == "storage_unavailable"
    fetcher.fetch.assert_not_called()


@pytest.mark.parametrize("during_parse",[False,True])
@pytest.mark.asyncio
async def test_operation_timeout_releases_capacity_and_does_not_abandon_parser(monkeypatch,during_parse):
    import time
    from threading import Event
    service,store,fetcher=provider();finished=Event()
    monkeypatch.setattr(rss,"OPERATION_DEADLINE",0.01)
    if during_parse:
        def slow_parser(data,feed):
            time.sleep(0.05);finished.set();return None,"rss2",[],0
        monkeypatch.setattr(rss,"parse_rss",slow_parser)
    else:
        async def wait(feed):await anyio.sleep_forever()
        fetcher.fetch.side_effect=wait
    result=await service.discover(request(),"app")
    assert result.status=="timeout" and not store.data and not store.leases
    assert rss._capacity.borrowed_tokens==0
    if during_parse:assert finished.is_set()


@pytest.fixture
def api_context(monkeypatch):
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.core.database import get_db
    from app.core.models import CoreBase, ApplicationDB, APIKeyDB
    from app.core import rate_limit
    from app.api.v1.router import api_router
    from app.api.v1.discovery import get_rss_provider
    from app.factory import create_app
    from app.services.api_key_service import generate_api_key
    from app.services.rate_limiter import RateLimitDecision
    engine = create_engine("sqlite://", connect_args={"check_same_thread":False}, poolclass=StaticPool)
    CoreBase.metadata.create_all(engine);sessions = sessionmaker(bind=engine)
    key = generate_api_key(environment="test")
    with sessions() as db:
        app_record = ApplicationDB(name="RSS contract", slug="rss-contract");db.add(app_record);db.flush()
        db.add(APIKeyDB(application_id=app_record.id, name="Offline", key_prefix=key.key_prefix,
            key_hash=key.key_hash, scopes=["discovery:read"]));db.commit()
    def writes(conn, cursor, statement, parameters, context, executemany):
        sql = statement.strip().lower()
        if sql.startswith(("insert", "update", "delete", "create", "drop", "alter")):
            assert sql.startswith("update core_api_keys set last_used_at="), "Unexpected discovery persistence"
    event.listen(engine, "before_cursor_execute", writes)
    def session():
        with sessions() as db:yield db
    service, store, fetcher = provider()
    app = create_app(routers=(api_router,));app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_rss_provider] = lambda:service
    limiter = AsyncMock(return_value=RateLimitDecision(True, 10, 9, 0))
    monkeypatch.setattr(rate_limit, "enforce_rate_limit", limiter)
    yield app, key.plaintext_key, service, store, fetcher, limiter
    from sqlalchemy import select, func
    from app.core.models import SourceRecordDB
    with sessions() as db:assert db.scalar(select(func.count()).select_from(SourceRecordDB)) == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_rss_api_pagination_authorized_no_persistence(api_context):
    app, key, _, _, fetcher, limiter = api_context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://core.test") as client:
        first = await client.post("/api/v1/discovery", headers={"X-API-Key":key}, json={"provider":"rss", "feed_id":"bbc-world", "limit":2})
        assert first.status_code == 200 and first.json()["status"] == "incomplete"
        next_page = await client.post("/api/v1/discovery", headers={"X-API-Key":key}, json={"provider":"rss", "feed_id":"bbc-world", "limit":2, "cursor":first.json()["next_cursor"]})
        assert next_page.status_code == 200 and next_page.json()["offset"] == 2
    assert [c.kwargs["bucket"] for c in limiter.call_args_list] == ["general", "discovery", "general", "rss_continuation"]
    fetcher.fetch.assert_awaited_once()
    assert "secret" not in first.text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,status", [("missing",401),("invalid",401),("scope",403),("general",429),("discovery",429),("redis",503)])
async def test_rss_api_auth_permissions_and_quotas(api_context, monkeypatch, mode, status):
    app, key, _, _, fetcher, limiter = api_context
    from app.core.permissions import require_scopes
    from fastapi import HTTPException
    if mode == "scope":
        from types import SimpleNamespace
        from app.core.auth import AuthenticatedApplication, get_current_application
        app.dependency_overrides[get_current_application] = lambda: AuthenticatedApplication(
            SimpleNamespace(id="app"), SimpleNamespace(id="key", scopes=["sources:read"]))
    elif mode in {"general","discovery"}:
        from app.services.rate_limiter import RateLimitDecision
        async def limit(**kwargs):return RateLimitDecision(kwargs["bucket"] != mode, 10, 0, 60)
        limiter.side_effect = limit
    elif mode == "redis":
        from redis.exceptions import ConnectionError
        limiter.side_effect = ConnectionError("isolated")
    headers = {} if mode == "missing" else {"X-API-Key":"invalid" if mode == "invalid" else key}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://core.test") as client:
        result = await client.post("/api/v1/discovery", headers=headers, json={"provider":"rss","feed_id":"bbc-world"})
    assert result.status_code == status;fetcher.fetch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,status", [({"provider":"rss","feed_id":"unknown"},422),
    ({"provider":"rss","feed_id":"bbc-world","url":"http://localhost"},422),
    ({"provider":"rss","feed_id":"bbc-world","cursor":"invalid"},400)])
async def test_rss_api_invalid_input_never_fetches(api_context, payload, status):
    app,key,_,_,fetcher,_=api_context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://core.test") as client:
        response = await client.post("/api/v1/discovery", headers={"X-API-Key":key}, json=payload)
    assert response.status_code == status;fetcher.fetch.assert_not_called()
