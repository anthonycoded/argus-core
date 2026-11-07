"""Characterize ingestion and its request-local deduplication; no live feeds."""

import importlib
import asyncio
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from app.config import FEEDS, KEYWORDS
from app.clients.argus_core_client import ArgusCoreClient, CoreClientError, RSSDiscoveryError, get_core_discovery_client
from app.global_chess_config import INGESTION_RSS_FEEDS
from app.database import SessionLocal, get_db
from app.global_chess_factory import create_app
from app.models import Topic
from app.routes import ingestion
from app.services import topic_scoring
from app.services.rss_service import parse_published, score_topic

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def entry(title="Ukraine NATO report", link="https://example.com/article", **extra):
    # Validated-client-shaped fixtures keep the original persistence/scoring cases.
    try:
        published = extra.get("published_at", parse_published(extra))
    except (ValueError, TypeError):
        published = extra.get("published_parsed")
    return dict(title=title, url=link, publisher="Core publisher", published_at=published,
                source_type="rss", snippet=extra.get("snippet"), provider_metadata={
                    "provider":"rss", "canonical_url":link, "domain":"example.com",
                    "provenance":{"entry_index":0,"original_url":link,"warnings":[]}})


def feed(*entries, bozo=False):
    if bozo:
        return RSSDiscoveryError(CoreClientError(502,
            "RSS feed is incomplete; some entries or metadata could not be accepted."), list(entries))
    return list(entries)


def utc(value):
    # SQLite drops timezone metadata; PostgreSQL preserves the aware instant.
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(httpcore.ConnectionPool, "handle_request", Mock(side_effect=AssertionError("Live HTTP")))
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", AsyncMock(side_effect=AssertionError("Live HTTP")))
    monkeypatch.setattr("feedparser.parse", Mock(side_effect=AssertionError("Direct RSS fallback")))
    monkeypatch.setattr("urllib.request.urlopen", Mock(side_effect=AssertionError("Live RSS")))
    monkeypatch.setattr(ingestion, "datetime", FrozenDatetime)
    monkeypatch.setattr(topic_scoring, "datetime", FrozenDatetime)
    monkeypatch.setattr(importlib.import_module("app.models.topic"), "datetime", FrozenDatetime)


@pytest.fixture(params=["sqlite", "postgresql"])
def storage(request):
    """Only memory SQLite or a verified disposable PG cluster; no app engine."""
    schema = None
    if request.param == "sqlite":
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        target = engine
    else:
        raw = os.getenv("ARGUS_INGESTION_TEST_DATABASE_URL")
        expected_cluster = os.getenv("ARGUS_INGESTION_TEST_PG_SYSTEM_ID")
        if not raw or not expected_cluster:
            pytest.skip("Requires an explicitly verified disposable ingestion PostgreSQL cluster.")
        url = make_url(raw)
        assert url.get_backend_name() == "postgresql" and url.host == "127.0.0.1"
        assert url.port not in {None, 5432, 5433} and url.database == "argus_core_test"
        engine = create_engine(url, poolclass=NullPool, connect_args={"connect_timeout": 3})
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_database()")) == "argus_core_test"
            assert str(connection.scalar(text("SELECT system_identifier FROM pg_control_system()"))) == expected_cluster
        schema = "argus_ingest_test_" + uuid4().hex
        assert re.fullmatch(r"argus_ingest_test_[0-9a-f]{32}", schema)
        with engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        target = engine.execution_options(schema_translate_map={None: schema})
    try:
        # Just the application Topic table, in memory or our new disposable schema.
        Topic.__table__.create(target)
        yield request.param, target
    finally:
        Topic.__table__.drop(target, checkfirst=True)
        if schema:
            with engine.begin() as connection:
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}"')
                assert not connection.scalar(text("SELECT 1 FROM pg_namespace WHERE nspname=:name"), {"name": schema})
        engine.dispose()


@pytest.fixture
def harness(storage, monkeypatch):
    backend, engine = storage
    state = SimpleNamespace(backend=backend, requests=[], statements=[], fail_after_flush=False, commit_error=None)

    class TracedSession(Session):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.operations = []

        def commit(self):
            self.operations.append("commit")
            if state.fail_after_flush:
                self.flush()
                raise RuntimeError("Isolated failure after actual SQL writes")
            try:
                return super().commit()
            except Exception as exc:
                state.commit_error = exc
                raise

        def flush(self, *args, **kwargs):
            self.operations.append("flush")
            return super().flush(*args, **kwargs)

        def rollback(self):
            self.operations.append("rollback")
            return super().rollback()

    state.sessions = sessionmaker(bind=engine, class_=TracedSession, autoflush=False, autocommit=False)
    def record(conn, cursor, statement, parameters, context, executemany):
        state.statements.append(statement.strip())
    event.listen(engine, "before_cursor_execute", record)
    discovery = AsyncMock()
    discovery.discover_rss.side_effect = [feed(), feed()]
    state.discover = discovery.discover_rss
    app = create_app(routers=(ingestion.router,))
    def session():
        with state.sessions() as db:
            state.requests.append(db)
            yield db
    app.dependency_overrides[get_db] = session
    app.dependency_overrides[get_core_discovery_client] = lambda: discovery
    state.app = app
    with TestClient(app) as client:
        state.client = client
        yield state


def rows(harness):
    with harness.sessions() as db:
        return [dict(id=t.id, title=t.title, url=t.url, source=t.source,
                     published_at=utc(t.published_at), fetched_at=utc(t.fetched_at),
                     relevance=t.relevance_score, freshness=t.freshness_score, score=t.score,
                     status=t.status, summary=t.summary) for t in db.scalars(select(Topic).order_by(Topic.id))]


def seed(harness, **changes):
    fields = dict(title="Previously saved", url="https://example.com/article", source="Original",
                  published_at=NOW-timedelta(hours=24), fetched_at=NOW-timedelta(days=1),
                  relevance_score=1, freshness_score=1, score=1, status="approved", summary="Keep editorial summary")
    fields.update(changes)
    with harness.sessions() as db:
        db.add(Topic(**fields)); db.commit()


def test_config_and_session_defaults():
    assert list(FEEDS.items()) == [("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
                                  ("NPR Politics", "https://feeds.npr.org/1014/rss.xml")]
    assert SessionLocal.kw["autoflush"] is False and SessionLocal.kw["autocommit"] is False


def test_ingest_contract_and_new_topic_defaults(harness):
    harness.discover.side_effect = [feed(entry(title="  Weather and sport  ", publisher="Ignored", summary="Not a Topic field")), feed()]
    assert "requestBody" not in harness.app.openapi()["paths"]["/ingest"]["post"]
    response = harness.client.post("/ingest?limit=0", json={"feed_url":"https://untrusted.invalid/rss"})
    assert response.status_code == 200 and response.json() == {"inserted":1,"updated":0,"feed_errors":[]}
    saved, = rows(harness)
    assert saved == dict(id=saved["id"], title="Weather and sport", url="https://example.com/article", source="BBC World",
                        published_at=None, fetched_at=NOW, relevance=0, freshness=40, score=12,
                        status="candidate", summary=None)
    assert harness.client.get("/ingest").status_code == 405
    assert [call.kwargs for call in harness.discover.call_args_list] == [{"feed_id":feed_id} for _, feed_id in INGESTION_RSS_FEEDS]
    assert harness.requests[0].operations == ["commit", "flush"]


@pytest.mark.parametrize("status", ["candidate", "approved", "rejected"])
def test_existing_topic_retains_editorial_state_and_erases_missing_date(harness, status):
    seed(harness, status=status)
    harness.discover.side_effect = [feed(entry(title="Government update")), feed()]
    response = harness.client.post("/ingest")
    assert response.json() == {"inserted":0,"updated":1,"feed_errors":[]}
    saved, = rows(harness)
    assert (saved["status"], saved["summary"]) == (status, "Keep editorial summary")
    assert (saved["title"], saved["source"], saved["published_at"], saved["fetched_at"]) == ("Government update", "BBC World", None, NOW)
    assert (saved["relevance"], saved["freshness"], saved["score"]) == (5,40,15.5)


def test_second_ingestion_updates_previously_committed_topic(harness):
    harness.discover.side_effect = [feed(entry()), feed(), feed(entry()), feed(entry())]
    assert harness.client.post("/ingest").json()["inserted"] == 1
    assert harness.client.post("/ingest").json() == {"inserted":0,"updated":1,"feed_errors":[]}
    assert len(rows(harness)) == 1


@pytest.mark.parametrize("across_feeds", [False, True])
def test_duplicate_new_normalized_url_inserts_once_and_preserves_other_work(harness, across_feeds):
    seed(harness, url="https://example.com/existing")
    first = entry(title="First observation", link="https://EXAMPLE.com/new/?utm_source=bbc")
    second = entry(title="Final government report", link="https://example.com/new#fragment",
                   published="Fri, 06 Nov 2026 12:00:00 GMT")
    existing = entry(title="Changed existing", link="https://example.com/existing")
    harness.discover.side_effect = [feed(existing, first), feed(second)] if across_feeds else [feed(existing, first, second), feed()]
    response = harness.client.post("/ingest")
    assert response.status_code == 200 and response.json() == {"inserted":1,"updated":1,"feed_errors":[]}
    old, new = rows(harness)
    assert old["title"] == "Changed existing" and old["summary"] == "Keep editorial summary" and old["status"] == "approved"
    assert new["url"] == "https://example.com/new" and new["title"] == "Final government report"
    assert new["source"] == ("NPR Politics" if across_feeds else "BBC World")
    assert (new["published_at"],new["relevance"],new["freshness"],new["score"]) == (NOW,5,100,33.5)
    assert new["status"] == "candidate" and new["summary"] is None
    assert harness.commit_error is None and harness.requests[-1].operations == ["commit", "flush"]


@pytest.mark.parametrize("across_feeds", [False, True])
@pytest.mark.parametrize("status", ["candidate", "approved", "rejected"])
def test_duplicate_existing_url_counts_once_and_last_one_wins(harness, across_feeds, status):
    seed(harness, status=status)
    first, last = entry(title="First Ukraine report"), entry(title="Final government report")
    harness.discover.side_effect = [feed(first), feed(last)] if across_feeds else [feed(first,last),feed()]
    response = harness.client.post("/ingest")
    assert response.json() == {"inserted":0,"updated":1,"feed_errors":[]}
    saved, = rows(harness)
    assert saved["title"] == "Final government report" and saved["source"] == ("NPR Politics" if across_feeds else "BBC World")
    assert (saved["status"],saved["summary"]) == (status,"Keep editorial summary")


def test_only_new_duplicate_observations_count_as_one_insert_and_no_updates(harness):
    harness.discover.side_effect = [feed(entry(),entry(title="Second observation")),feed(entry(title="Final government report"))]
    response = harness.client.post("/ingest")
    assert response.json() == {"inserted":1,"updated":0,"feed_errors":[]}
    saved, = rows(harness)
    assert saved["title"] == "Final government report" and saved["source"] == "NPR Politics"
    assert (saved["relevance"],saved["freshness"],saved["score"]) == (5,40,15.5)


def test_mixed_urls_count_distinct_new_and_existing_topics(harness):
    seed(harness, url="https://example.com/b")
    seed(harness, url="https://example.com/d", status="rejected", summary="Rejected summary")
    harness.discover.side_effect = [feed(
        entry(link="https://example.com/a?utm_source=bbc"), entry(link="https://example.com/b"),
        entry(link="https://example.com/a/"), entry(link="https://example.com/c")),feed(
        entry(title="Election Congress",link="https://example.com/b#rss",published="Thu, 05 Nov 2026 00:00:00 GMT"),
        entry(title="Final government report",link="https://example.com/a",published="Fri, 06 Nov 2026 12:00:00 GMT"),
        entry(link="https://example.com/d"),entry(link="https://example.com/c"),entry(link="https://example.com/e"))]
    harness.statements.clear()
    response = harness.client.post("/ingest")
    assert response.json() == {"inserted":3,"updated":2,"feed_errors":[]}
    # One lookup per distinct normalized URL; no flush between observations.
    assert sum(sql.upper().startswith("SELECT") for sql in harness.statements) == 5
    saved = {row["url"].rsplit("/",1)[-1]:row for row in rows(harness)}
    assert len(saved) == 5 and saved["a"]["source"] == saved["b"]["source"] == "NPR Politics"
    assert (saved["a"]["published_at"],saved["a"]["relevance"],saved["a"]["freshness"],saved["a"]["score"]) == (NOW,5,100,33.5)
    assert (saved["b"]["relevance"],saved["b"]["freshness"],saved["b"]["score"]) == (20,50,29)
    assert (saved["b"]["status"],saved["b"]["summary"]) == ("approved","Keep editorial summary")
    assert (saved["d"]["status"],saved["d"]["summary"]) == ("rejected","Rejected summary")
    assert all(saved[key]["status"] == "candidate" and saved[key]["summary"] is None for key in ("a","c","e"))


@pytest.mark.parametrize("already_exists", [False, True])
@pytest.mark.parametrize("invalid", ["missing_title", "malformed_date"])
def test_invalid_later_duplicate_cannot_replace_last_valid_observation(harness, already_exists, invalid):
    if already_exists:seed(harness)
    bad = entry(title=None) if invalid == "missing_title" else entry(published_parsed=(2026,))
    harness.discover.side_effect = [feed(entry()),feed(
        entry(title="Final government report",published="Fri, 06 Nov 2026 12:00:00 GMT"),bad)]
    response = harness.client.post("/ingest")
    data = response.json()
    assert response.status_code == 200 and (data["inserted"],data["updated"]) == (int(not already_exists),int(already_exists))
    assert len(data["feed_errors"]) == 1
    saved, = rows(harness)
    assert saved["title"] == "Final government report" and saved["source"] == "NPR Politics"
    assert (saved["published_at"],saved["relevance"],saved["freshness"],saved["score"]) == (NOW,5,100,33.5)


def test_database_unique_url_protection_is_unchanged(harness):
    with harness.sessions() as db:
        for title in ("One", "Two"):
            db.add(Topic(title=title,url="https://example.com/same",source="BBC World",
                         relevance_score=0,freshness_score=40,score=12))
        with pytest.raises(IntegrityError) as error:
            db.commit()
        if harness.backend == "postgresql":assert error.value.orig.sqlstate == "23505"
        db.rollback()
    assert rows(harness) == []


@pytest.mark.parametrize("which", ["title", "link"])
@pytest.mark.parametrize("missing", [None, "", "   "])
def test_missing_or_blank_required_fields_are_silently_skipped(harness, which, missing):
    invalid = entry();invalid["url" if which == "link" else which] = missing
    harness.discover.side_effect = [feed(invalid,entry(link="https://example.com/valid")),feed()]
    result = harness.client.post("/ingest").json()
    assert result["inserted"] == 1 and result["updated"] == 0 and len(result["feed_errors"]) == 1
    assert rows(harness)[0]["url"] == "https://example.com/valid"


@pytest.mark.parametrize("mode", ["exception", "incomplete_empty", "incomplete_with_entries", "empty", "partial_failure", "invalid_candidate"])
def test_partial_feed_failures_retain_valid_observations(harness, mode):
    good = entry(link="https://example.com/bbc")
    if mode == "exception": first = RuntimeError("private transport diagnostic")
    elif mode == "incomplete_empty": first = feed(bozo=True)
    elif mode == "incomplete_with_entries": first = feed(good,bozo=True)
    elif mode == "empty": first = feed()
    elif mode == "partial_failure": first = RSSDiscoveryError(CoreClientError(410,"expired"),[good])
    else: first = feed(good,entry(link="http://[broken"),entry(link="https://example.com/later-valid"))
    harness.discover.side_effect = [first,feed(entry(link="https://example.com/npr"))]
    response = harness.client.post("/ingest")
    assert response.status_code == 200
    data = response.json()
    expected = 3 if mode == "invalid_candidate" else 2 if mode in {"incomplete_with_entries","partial_failure"} else 1
    assert data["inserted"] == expected and data["updated"] == 0
    assert len(data["feed_errors"]) == int(mode != "empty")
    if data["feed_errors"]:assert data["feed_errors"][0]["source"] == "BBC World"
    assert "private" not in response.text
    assert rows(harness)[-1]["source"] == "NPR Politics"
    assert harness.requests[-1].operations == ["commit","flush"]


def test_all_feeds_fail_still_returns_200_with_sanitized_errors(harness):
    harness.discover.side_effect = [RuntimeError("private"*100),CoreClientError(503,"private")]
    response = harness.client.post("/ingest")
    assert response.status_code == 200 and response.json() == dict(inserted=0,updated=0,feed_errors=[
        {"source":"BBC World","error":"RSS discovery failed; coverage is incomplete."},
        {"source":"NPR Politics","error":"RSS discovery service unavailable."}])
    assert rows(harness) == [] and harness.requests[-1].operations == ["commit"]


def test_real_sql_writes_rollback_if_commit_fails(harness):
    seed(harness);before = rows(harness)
    harness.discover.side_effect = [feed(entry(title="Updated"),entry(link="https://example.com/new")),
                                feed(entry(title="Last update"),entry(title="Last new",link="https://example.com/new"))]
    harness.statements.clear();harness.fail_after_flush = True
    response = harness.client.post("/ingest")
    assert response.status_code == 500 and response.json() == {"detail":"Database write failed during ingestion"}
    assert any(sql.upper().startswith("INSERT") for sql in harness.statements)
    assert any(sql.upper().startswith("UPDATE") for sql in harness.statements)
    assert rows(harness) == before
    assert harness.requests[-1].operations == ["commit","flush","rollback"]


def test_no_sql_insert_occurs_between_feed_reads(harness):
    async def discover(*, feed_id):
        assert not any(sql.upper().startswith(("INSERT","UPDATE")) for sql in harness.statements)
        if feed_id == INGESTION_RSS_FEEDS[0][1]:return feed(entry())
        assert len(harness.requests[-1].new) == 0
        return feed()
    harness.discover.side_effect = discover
    assert harness.client.post("/ingest").json()["inserted"] == 1


def test_title_is_scored_before_storage_truncation(harness):
    harness.discover.side_effect = [feed(entry(title="x"*501+" election")),feed()]
    assert harness.client.post("/ingest").status_code == 200
    saved, = rows(harness)
    assert saved["title"] == "x"*500 and saved["relevance"] == 10 and saved["score"] == 19


@pytest.mark.parametrize("link", ["relative/path", "ftp://example.com/article", "http://127.0.0.1/article"])
def test_unsafe_client_candidates_are_rejected(harness, link):
    harness.discover.side_effect = [feed(entry(link=link)),feed()]
    result = harness.client.post("/ingest").json()
    assert result["inserted"] == result["updated"] == 0 and len(result["feed_errors"]) == 1
    assert rows(harness) == []


def test_oversized_urls_are_rejected_before_writes(harness):
    url="https://example.com/"+"x"*2048
    harness.discover.side_effect = [feed(entry(link=url)),feed()]
    response=harness.client.post("/ingest")
    assert response.status_code == 200 and len(response.json()["feed_errors"]) == 1
    assert rows(harness) == []


@pytest.mark.parametrize("value,expected", [
    ({},None), ({"published":"not a date"},None),
    ({"published":"Fri, 06 Nov 2026 07:00:00 -0500"},NOW),
    ({"updated":"Fri, 06 Nov 2026 12:00:00 GMT"},NOW),
    ({"published":"Fri, 06 Nov 2026 12:00:00"},NOW),
    ({"updated_parsed":(2026,11,6,12,0,0,4,310,0)},None),
    ({"published":"2026-11-06T12:00:00Z"},None),
    ({"published":"broken","updated":"Fri, 06 Nov 2026 12:00:00 GMT"},None),
    ({"published_parsed":(2026,11,6,12,0,0,4,310,0),"published":"broken"},NOW),
])
def test_legacy_parser_and_typed_ingestion_dates(harness,value,expected):
    assert parse_published(value) == expected
    harness.discover.side_effect=[feed(entry(**value)),feed()]
    assert harness.client.post("/ingest").json()["inserted"] == 1
    saved,=rows(harness)
    assert saved["published_at"] == expected and saved["freshness"] == (100 if expected else 40)


@pytest.mark.parametrize("title,published,relevance,freshness,total", [
    ("Weather",None,0,40,12), ("ELECTION election",None,10,40,19),
    ("NATO congressman",NOW,20,100,44),  # 'congress' matches inside a word.
    ("Ukraine NATO",NOW-timedelta(hours=36),16,50,26.2),
    ("Ukraine NATO",NOW-timedelta(hours=72),16,0,11.2),
    ("Ukraine NATO",NOW+timedelta(days=1),16,100,41.2),
    (" ".join(KEYWORDS),None,100,40,82),
])
def test_scoring_policy(title,published,relevance,freshness,total):
    assert score_topic(title,published) == (relevance,freshness,total)


def wire_candidate(index, title="Weather report", url=None):
    url = url or f"https://example.com/{index}"
    return dict(provider="rss",source_type="rss",title=title,url=url,canonical_url=url,
        domain="example.com",publisher="Provider publisher",published_at="2026-11-06T14:00:00+02:00",snippet="Supplied snippet",
        provenance=dict(entry_index=index,original_url=f"/original/{index}",warnings=[]))


def wire_page(feed_id="bbc-world", offset=0, total=76):
    end = min(offset+75,total)
    return dict(provider="rss",feed_id=feed_id,feed_label=dict((i,s) for s,i in INGESTION_RSS_FEEDS)[feed_id],
        feed_url="https://feeds.bbci.co.uk/news/world/rss.xml" if feed_id=="bbc-world" else "https://feeds.npr.org/1014/rss.xml",
        feed_revision="1",feed_title="Supplied feed",format="rss2",status="success",
        candidates=[wire_candidate(i) for i in range(offset,end)],rejected_entries=[],snapshot_id="a"*32,
        fetched_at="2026-11-06T12:00:00Z",expires_at="2026-11-06T12:05:00Z",total_entries=total,
        invalid_entries=0,duplicate_observations=0,offset=offset,entries_in_page=end-offset,
        has_more=end<total,next_cursor=f"cursor-{end}" if end<total else None,error=None,retry_after=None)


async def core_ingest(harness, respond):
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as core:
        client = ArgusCoreClient(base_url="https://core.test",api_key="test-only-ingestion-key",request_timeout=4,http_client=core)
        harness.app.dependency_overrides[get_core_discovery_client] = lambda:client
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app),base_url="http://gc.test") as gc:
            return await gc.post("/ingest")


@pytest.mark.asyncio
async def test_authenticated_snapshot_pagination_persistence_and_provenance(harness,monkeypatch):
    seed(harness,url="https://example.com/0");harness.statements.clear()
    calls=[];batches=[];event_loop_thread=threading.get_ident()
    persist=ingestion._persist_topics
    def save(feeds,db):
        assert threading.get_ident()!=event_loop_thread
        batches.extend(feeds)
        assert len(calls)==3
        return persist(feeds,db)
    monkeypatch.setattr(ingestion,"_persist_topics",save)
    def respond(request):
        from app.services.discovery.rss_contracts import RSSDiscoveryRequest
        payload=json.loads(request.content);RSSDiscoveryRequest.model_validate(payload)
        assert request.url=="https://core.test/api/v1/discovery" and request.method=="POST"
        assert request.headers["X-API-Key"]=="test-only-ingestion-key"
        assert all(value==4 for value in request.extensions["timeout"].values())
        assert not any(sql.upper().startswith(("SELECT","INSERT","UPDATE")) for sql in harness.statements)
        calls.append(payload)
        if payload["feed_id"]=="npr-politics":
            result=wire_page("npr-politics",total=2)
            result["candidates"]=[wire_candidate(0,"Final NATO Congress report"),wire_candidate(1,url="https://example.com/npr")]
        else:
            result=wire_page(offset=75 if payload["cursor"] else 0)
            result["duplicate_observations"]=1
            if payload["cursor"]:result["candidates"]=[wire_candidate(75,"Government later-page report",url="https://example.com/0")]
        return httpx.Response(200,json=result)
    response=await core_ingest(harness,respond)
    assert response.json()=={"inserted":75,"updated":1,"feed_errors":[]}
    assert [(c["feed_id"],c["cursor"]) for c in calls]==[("bbc-world",None),("bbc-world","cursor-75"),("npr-politics",None)]
    assert [len(batch.observations) for batch in batches]==[76,2]
    last=batches[0].observations[-1]
    assert last.candidate.provenance.entry_index==75 and last.candidate.provenance.original_url=="/original/75"
    assert last.provider_metadata["snapshot_id"]=="a"*32 and last.provider_metadata["duplicate_observations"]==1
    saved={row["url"]:row for row in rows(harness)}
    winner=saved["https://example.com/0"]
    assert (winner["source"],winner["status"],winner["summary"])==("NPR Politics","approved","Keep editorial summary")
    assert (winner["published_at"],winner["relevance"],winner["freshness"],winner["score"])==(NOW,20,100,44)
    assert harness.requests[-1].operations==["commit","flush"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault",[401,403,410,429,503,504,"timeout","json","snapshot","cursor"])
async def test_later_page_errors_retain_partial_topics_without_retry(harness,fault):
    calls=[]
    def respond(request):
        payload=json.loads(request.content);calls.append(payload)
        if payload["feed_id"]=="npr-politics":return httpx.Response(200,json=wire_page("npr-politics",total=0))
        total=151 if fault=="cursor" else 76
        if payload["cursor"] is None:return httpx.Response(200,json=wire_page(total=total))
        if isinstance(fault,int):return httpx.Response(fault,text="private upstream credential",headers={"Retry-After":"007"})
        if fault=="timeout":raise httpx.ReadTimeout("private diagnostic")
        if fault=="json":return httpx.Response(200,content=b"not JSON")
        result=wire_page(offset=75,total=total)
        if fault=="snapshot":result["snapshot_id"]="b"*32
        else:result["next_cursor"]="cursor-75"
        return httpx.Response(200,json=result)
    response=await core_ingest(harness,respond)
    data=response.json()
    assert response.status_code==200 and data["inserted"]==75 and data["updated"]==0 and len(data["feed_errors"])==1
    assert data["feed_errors"][0]["source"]=="BBC World" and len(calls)==3 and len(rows(harness))==75
    assert "private" not in response.text and "test-only-ingestion-key" not in response.text
    assert ("Retry-After: 7" in response.text)==(fault in {429,503})
    if fault==410:assert "expired" in response.text
    if fault==401:assert "authentication" in response.text
    if fault==403:assert "permission" in response.text


@pytest.mark.asyncio
async def test_incomplete_snapshot_retains_accepted_entries_and_reports_coverage(harness):
    def respond(request):
        payload=json.loads(request.content)
        result=wire_page(payload["feed_id"],total=2 if payload["feed_id"]=="bbc-world" else 0)
        if payload["feed_id"]=="bbc-world":
            result.update(status="incomplete",invalid_entries=1,candidates=[wire_candidate(0)],
                          rejected_entries=[{"entry_index":1,"reason":"missing_title"}])
        return httpx.Response(200,json=result)
    response=await core_ingest(harness,respond)
    assert response.json()["inserted"]==1 and len(response.json()["feed_errors"])==1 and "incomplete" in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("change",[{"url":"http://127.0.0.1/private"},{"url":"https://example.com/"+"x"*2048},
    {"title":"x"*8193},{"title":"invalid\x00title"},{"snippet":"x"*65537},
    {"url":"https://example.com?q="+"x"*(2048-len("https://example.com?q="))},
    {"published_at":"2026-11-06T12:00:00"}])
async def test_invalid_core_candidates_never_reach_database(harness,change):
    def respond(request):
        feed_id=json.loads(request.content)["feed_id"]
        result=wire_page(feed_id,total=1 if feed_id=="bbc-world" else 0)
        if result["candidates"]:result["candidates"][0].update(change)
        return httpx.Response(200,json=result)
    response=await core_ingest(harness,respond)
    assert response.json()["inserted"]==response.json()["updated"]==0
    assert len(response.json()["feed_errors"])==1 and rows(harness)==[]


@pytest.mark.asyncio
async def test_complete_500_observation_snapshot_is_not_truncated(harness):
    calls=[]
    def respond(request):
        payload=json.loads(request.content);calls.append(payload)
        offset=int(payload["cursor"].split("-")[1]) if payload["cursor"] else 0
        return httpx.Response(200,json=wire_page(payload["feed_id"],offset,500 if payload["feed_id"]=="bbc-world" else 0))
    response=await core_ingest(harness,respond)
    assert response.json()=={"inserted":500,"updated":0,"feed_errors":[]} and len(calls)==8
    assert len(rows(harness))==500


@pytest.mark.asyncio
async def test_collection_deadline_retains_prior_pages(harness,monkeypatch):
    from app.clients import argus_core_client
    monkeypatch.setattr(argus_core_client,"RSS_COLLECTION_TIMEOUT",0.1)
    calls=[]
    async def respond(request):
        payload=json.loads(request.content);calls.append(payload)
        if payload["feed_id"]=="npr-politics":return httpx.Response(200,json=wire_page("npr-politics",total=0))
        if payload["cursor"]:await asyncio.sleep(1)
        return httpx.Response(200,json=wire_page())
    response=await core_ingest(harness,respond)
    assert response.json()["inserted"]==75 and "timed out" in response.text and len(calls)==3


@pytest.mark.asyncio
async def test_empty_success_and_invalid_retry_guidance(harness):
    response=await core_ingest(harness,lambda request:httpx.Response(200,json=wire_page(json.loads(request.content)["feed_id"],total=0)))
    assert response.json()=={"inserted":0,"updated":0,"feed_errors":[]}
    response=await core_ingest(harness,lambda _:httpx.Response(429,headers={"Retry-After":"private invalid header"}))
    assert len(response.json()["feed_errors"])==2 and "Retry-After" not in response.text and "private" not in response.text


@pytest.mark.asyncio
async def test_oversized_snapshot_is_explicit_failure(harness):
    def respond(request):
        feed_id=json.loads(request.content)["feed_id"]
        return httpx.Response(200,json=wire_page(feed_id,total=501 if feed_id=="bbc-world" else 0))
    response=await core_ingest(harness,respond)
    assert response.json()["inserted"]==0 and len(response.json()["feed_errors"])==1 and rows(harness)==[]


@pytest.mark.asyncio
async def test_core_http_success_does_not_weaken_commit_rollback(harness):
    seed(harness,url="https://example.com/0");before=rows(harness)
    harness.fail_after_flush=True
    def respond(request):
        return httpx.Response(200,json=wire_page(json.loads(request.content)["feed_id"],total=2))
    response=await core_ingest(harness,respond)
    assert response.status_code==500 and response.json()=={"detail":"Database write failed during ingestion"}
    assert rows(harness)==before and harness.requests[-1].operations==["commit","flush","rollback"]
