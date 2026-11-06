"""Characterize ingestion and its request-local deduplication; no live feeds."""

import importlib
import os
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpcore
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from app.config import FEEDS, KEYWORDS
from app.core.database import SessionLocal, get_db
from app.factory import create_app
from app.models import Topic
from app.routes import ingestion
from app.services import topic_scoring
from app.services.rss_service import normalize_url, parse_published, score_topic

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)


def entry(title="Ukraine NATO report", link="https://example.com/article", **extra):
    return dict(title=title, link=link, **extra)


def feed(*entries, bozo=False):
    return SimpleNamespace(entries=list(entries), bozo=bozo)


def utc(value):
    # SQLite drops timezone metadata; PostgreSQL preserves the aware instant.
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(httpcore.ConnectionPool, "handle_request", Mock(side_effect=AssertionError("Live HTTP")))
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", AsyncMock(side_effect=AssertionError("Live HTTP")))
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
    mocked_parse = Mock(side_effect=[feed(), feed()])
    monkeypatch.setattr(ingestion.feedparser, "parse", mocked_parse)
    state.parse = mocked_parse
    app = create_app(routers=(ingestion.router,))
    def session():
        with state.sessions() as db:
            state.requests.append(db)
            yield db
    app.dependency_overrides[get_db] = session
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
    harness.parse.side_effect = [feed(entry(title="  Weather and sport  ", publisher="Ignored", summary="Not a Topic field")), feed()]
    assert "requestBody" not in harness.app.openapi()["paths"]["/ingest"]["post"]
    response = harness.client.post("/ingest?limit=0", json={"feed_url":"https://untrusted.invalid/rss"})
    assert response.status_code == 200 and response.json() == {"inserted":1,"updated":0,"feed_errors":[]}
    saved, = rows(harness)
    assert saved == dict(id=saved["id"], title="Weather and sport", url="https://example.com/article", source="BBC World",
                        published_at=None, fetched_at=NOW, relevance=0, freshness=40, score=12,
                        status="candidate", summary=None)
    assert harness.client.get("/ingest").status_code == 405
    assert [call.args for call in harness.parse.call_args_list] == [(url,) for url in FEEDS.values()]
    assert all(not call.kwargs for call in harness.parse.call_args_list)  # No explicit fetch timeout/options.
    assert harness.requests[0].operations == ["commit", "flush"]


@pytest.mark.parametrize("status", ["candidate", "approved", "rejected"])
def test_existing_topic_retains_editorial_state_and_erases_missing_date(harness, status):
    seed(harness, status=status)
    harness.parse.side_effect = [feed(entry(title="Government update")), feed()]
    response = harness.client.post("/ingest")
    assert response.json() == {"inserted":0,"updated":1,"feed_errors":[]}
    saved, = rows(harness)
    assert (saved["status"], saved["summary"]) == (status, "Keep editorial summary")
    assert (saved["title"], saved["source"], saved["published_at"], saved["fetched_at"]) == ("Government update", "BBC World", None, NOW)
    assert (saved["relevance"], saved["freshness"], saved["score"]) == (5,40,15.5)


def test_second_ingestion_updates_previously_committed_topic(harness):
    harness.parse.side_effect = [feed(entry()), feed(), feed(entry()), feed(entry())]
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
    harness.parse.side_effect = [feed(existing, first), feed(second)] if across_feeds else [feed(existing, first, second), feed()]
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
    harness.parse.side_effect = [feed(first), feed(last)] if across_feeds else [feed(first,last),feed()]
    response = harness.client.post("/ingest")
    assert response.json() == {"inserted":0,"updated":1,"feed_errors":[]}
    saved, = rows(harness)
    assert saved["title"] == "Final government report" and saved["source"] == ("NPR Politics" if across_feeds else "BBC World")
    assert (saved["status"],saved["summary"]) == (status,"Keep editorial summary")


def test_only_new_duplicate_observations_count_as_one_insert_and_no_updates(harness):
    harness.parse.side_effect = [feed(entry(),entry(title="Second observation")),feed(entry(title="Final government report"))]
    response = harness.client.post("/ingest")
    assert response.json() == {"inserted":1,"updated":0,"feed_errors":[]}
    saved, = rows(harness)
    assert saved["title"] == "Final government report" and saved["source"] == "NPR Politics"
    assert (saved["relevance"],saved["freshness"],saved["score"]) == (5,40,15.5)


def test_mixed_urls_count_distinct_new_and_existing_topics(harness):
    seed(harness, url="https://example.com/b")
    seed(harness, url="https://example.com/d", status="rejected", summary="Rejected summary")
    harness.parse.side_effect = [feed(
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
    harness.parse.side_effect = [feed(entry()),feed(
        entry(title="Final government report",published="Fri, 06 Nov 2026 12:00:00 GMT"),bad)]
    response = harness.client.post("/ingest")
    data = response.json()
    assert response.status_code == 200 and (data["inserted"],data["updated"]) == (int(not already_exists),int(already_exists))
    assert len(data["feed_errors"]) == int(invalid == "malformed_date")
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
    invalid = entry();invalid[which] = missing
    harness.parse.side_effect = [feed(invalid,entry(link="https://example.com/valid")),feed()]
    assert harness.client.post("/ingest").json() == {"inserted":1,"updated":0,"feed_errors":[]}
    assert rows(harness)[0]["url"] == "https://example.com/valid"


@pytest.mark.parametrize("mode", ["exception", "bozo_empty", "bozo_with_entries", "empty",
                                 "bad_entry_mid_feed", "bad_title_mid_feed", "bad_date_mid_feed"])
def test_partial_feed_failures_have_no_feed_rollback(harness, mode):
    good = entry(link="https://example.com/bbc")
    if mode == "exception": first = RuntimeError("mock transport failure")
    elif mode == "bozo_empty": first = feed(bozo=True)
    elif mode == "bozo_with_entries": first = feed(good,bozo=True)
    elif mode == "empty": first = feed()
    else:
        bad = (entry(link="http://[broken") if mode == "bad_entry_mid_feed" else
               entry(title=7) if mode == "bad_title_mid_feed" else entry(published_parsed=(2026,)))
        first = feed(good,bad,entry(link="https://example.com/not-reached"))
    harness.parse.side_effect = [first,feed(entry(link="https://example.com/npr"))]
    response = harness.client.post("/ingest")
    assert response.status_code == 200
    data = response.json();has_bbc = mode == "bozo_with_entries" or mode.startswith("bad_")
    assert data["inserted"] == 1+int(has_bbc) and data["updated"] == 0
    assert len(data["feed_errors"]) == int(mode in {"exception","bozo_empty"} or mode.startswith("bad_"))
    if data["feed_errors"]:assert data["feed_errors"][0]["source"] == "BBC World"
    assert [r["source"] for r in rows(harness)] == (["BBC World","NPR Politics"] if has_bbc else ["NPR Politics"])
    assert harness.requests[-1].operations == ["commit","flush"]


def test_all_feeds_fail_still_returns_200_and_truncates_raw_error(harness):
    harness.parse.side_effect = [RuntimeError("x"*600),feed(bozo=True)]
    response = harness.client.post("/ingest")
    assert response.status_code == 200 and response.json() == dict(inserted=0,updated=0,feed_errors=[
        {"source":"BBC World","error":"x"*300},{"source":"NPR Politics","error":"Feed could not be parsed"}])
    assert rows(harness) == [] and harness.requests[-1].operations == ["commit"]


def test_real_sql_writes_rollback_if_commit_fails(harness):
    seed(harness);before = rows(harness)
    harness.parse.side_effect = [feed(entry(title="Updated"),entry(link="https://example.com/new")),
                                feed(entry(title="Last update"),entry(title="Last new",link="https://example.com/new"))]
    harness.statements.clear();harness.fail_after_flush = True
    response = harness.client.post("/ingest")
    assert response.status_code == 500 and response.json() == {"detail":"Database write failed during ingestion"}
    assert any(sql.upper().startswith("INSERT") for sql in harness.statements)
    assert any(sql.upper().startswith("UPDATE") for sql in harness.statements)
    assert rows(harness) == before
    assert harness.requests[-1].operations == ["commit","flush","rollback"]


def test_no_sql_insert_occurs_between_feed_reads(harness):
    def parse(url):
        assert not any(sql.upper().startswith(("INSERT","UPDATE")) for sql in harness.statements)
        if url == next(iter(FEEDS.values())):return feed(entry())
        assert len(harness.requests[-1].new) == 1
        return feed()
    harness.parse.side_effect = parse
    assert harness.client.post("/ingest").json()["inserted"] == 1


def test_title_is_scored_before_storage_truncation(harness):
    harness.parse.side_effect = [feed(entry(title="x"*501+" election")),feed()]
    assert harness.client.post("/ingest").status_code == 200
    saved, = rows(harness)
    assert saved["title"] == "x"*500 and saved["relevance"] == 10 and saved["score"] == 19


@pytest.mark.parametrize("link", ["relative/path", "ftp://example.com/article", "http://127.0.0.1/article"])
def test_link_scheme_and_destination_are_not_validated(harness, link):
    harness.parse.side_effect = [feed(entry(link=link)),feed()]
    assert harness.client.post("/ingest").json()["inserted"] == 1
    assert rows(harness)[0]["url"] == normalize_url(link)


def test_oversized_url_is_left_to_backend_constraints(harness):
    url="https://example.com/"+"x"*2048
    harness.parse.side_effect = [feed(entry(link=url)),feed()]
    response=harness.client.post("/ingest")
    if harness.backend == "postgresql":
        assert response.status_code == 500 and rows(harness) == []
    else:
        assert response.status_code == 200 and rows(harness)[0]["url"] == url


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
def test_publication_date_parsing_and_persistence(harness,value,expected):
    assert parse_published(value) == expected
    harness.parse.side_effect=[feed(entry(**value)),feed()]
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
