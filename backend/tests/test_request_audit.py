from datetime import datetime, timezone
from unittest.mock import AsyncMock
from threading import Event
from time import perf_counter

import anyio

import pytest
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.database import get_db
from app.core.audit import RequestAuditMiddleware
from app.core.models import APIKeyDB, ApplicationDB, AuditLogDB, CoreBase
from app.core.permissions import require_scopes
from app.core.rate_limit import require_rate_limit
from app.factory import create_app
from app.services.api_key_service import generate_api_key
from app.services.rate_limiter import RateLimit, RateLimitDecision


@pytest.fixture
def audit_app(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    CoreBase.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    generated = generate_api_key()
    with sessions() as db:
        application = ApplicationDB(name="Audit test", slug="audit-test")
        db.add(application)
        db.flush()
        key = APIKeyDB(application_id=application.id, name="Test", key_prefix=generated.key_prefix,
                       key_hash=generated.key_hash, scopes=["sources:read"])
        db.add(key)
        db.commit()
        ids = (application.id, key.id)

    audit_calls = []
    failure = {"enabled": False}

    def audit_sessions():
        audit_calls.append(True)
        db = sessions()
        if failure["enabled"]:
            def fail_commit():
                raise RuntimeError("sensitive database connection or key detail")
            db.commit = fail_commit
        return db

    def request_db():
        with sessions() as db:
            yield db

    monkeypatch.setattr("app.core.rate_limit.enforce_rate_limit", AsyncMock(
        return_value=RateLimitDecision(False, 1, 0, 30)))
    router = APIRouter(prefix="/api/v1")

    @router.get("/items/{item_id}")
    def success(item_id: str, auth=Depends(require_scopes("sources:read"))):
        return {"status": "ok"}

    @router.post("/items/{item_id}")
    def permission_failure(item_id: str, auth=Depends(require_scopes("sources:write"))):
        return {"status": "ok"}

    @router.get("/limited")
    def limited(auth=Depends(require_rate_limit(bucket="audit-test", rate_limit=RateLimit(1, 60)))):
        return {"status": "ok"}

    @router.get("/explode")
    def explode(auth=Depends(require_scopes("sources:read")), db=Depends(get_db)):
        db.add(ApplicationDB(name="Must roll back", slug="uncommitted"))
        db.flush()
        raise RuntimeError("private application error")

    @router.get("/health")
    def health():
        return {"status": "ok"}

    app = create_app(routers=(router,), audit_session_factory=audit_sessions)
    app.dependency_overrides[get_db] = request_db
    app.get("/docs-probe")(lambda: {"status": "ok"})
    yield app, sessions, generated.plaintext_key, ids, audit_calls, failure
    engine.dispose()


def rows(sessions):
    with sessions() as db:
        return db.scalars(select(AuditLogDB)).all()


def test_authenticated_request_persists_one_sanitized_audit(audit_app):
    app, sessions, key, ids, calls, _ = audit_app
    before = datetime.now(timezone.utc)
    with TestClient(app) as client:
        response = client.get(f"/api/v1/items/{key}?api_key={key}&token=query-secret",
                              headers={"X-API-Key": key, "Authorization": "Bearer header-secret",
                                       "User-Agent": "sensitive-user-agent"})
    assert response.json() == {"status": "ok"}
    audit, = rows(sessions)
    assert (audit.application_id, audit.api_key_id) == ids
    assert (audit.method, audit.path, audit.status_code) == ("GET", "/api/v1/items/{item_id}", 200)
    assert audit.duration_ms >= 0
    assert audit.created_at.replace(tzinfo=timezone.utc) >= before
    assert len(calls) == 1
    persisted = repr({column.name: getattr(audit, column.key) for column in AuditLogDB.__table__.columns})
    for secret in (key, "query-secret", "header-secret", "sensitive-user-agent"):
        assert secret not in persisted


@pytest.mark.parametrize("key", [None, "invalid-plaintext-secret", "known-prefix-invalid"])
def test_authentication_failure_is_anonymous(audit_app, key):
    app, sessions, valid_key, _, calls, _ = audit_app
    if key == "known-prefix-invalid":
        key = valid_key[:16] + "wrong-secret"
    with TestClient(app) as client:
        response = client.get("/api/v1/items/1", headers={"X-API-Key": key} if key else {})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "APIKey"
    audit, = rows(sessions)
    assert audit.status_code == 401
    assert audit.application_id is None and audit.api_key_id is None
    assert key is None or key not in repr(audit.__dict__)
    assert len(calls) == 1


def test_permission_failure_retains_ids_and_does_not_store_body(audit_app):
    app, sessions, key, ids, _, _ = audit_app
    with TestClient(app) as client:
        response = client.post("/api/v1/items/secret-path", headers={"X-API-Key": key},
                               json={"secret": "body-secret"})
    assert response.status_code == 403
    assert response.json() == {"detail": "Insufficient API permissions."}
    audit, = rows(sessions)
    assert (audit.application_id, audit.api_key_id) == ids
    assert (audit.status_code, audit.method, audit.path) == (403, "POST", "/api/v1/items/{item_id}")


def test_rate_limit_response_preserved_and_logged(audit_app):
    app, sessions, key, ids, calls, _ = audit_app
    with TestClient(app) as client:
        response = client.get("/api/v1/limited", headers={"X-API-Key": key})
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "30"
    audit, = rows(sessions)
    assert audit.status_code == 429
    assert (audit.application_id, audit.api_key_id) == ids
    assert len(calls) == 1


@pytest.mark.parametrize("raise_errors", [False, True])
def test_unhandled_error_is_logged_independent_of_endpoint_transaction(audit_app, raise_errors):
    app, sessions, key, ids, calls, _ = audit_app
    with TestClient(app, raise_server_exceptions=raise_errors) as client:
        if raise_errors:
            with pytest.raises(RuntimeError, match="private application error"):
                client.get("/api/v1/explode", headers={"X-API-Key": key})
        else:
            response = client.get("/api/v1/explode", headers={"X-API-Key": key})
            assert response.status_code == 500 and response.text == "Internal Server Error"
    audit, = rows(sessions)
    assert audit.status_code == 500
    assert (audit.application_id, audit.api_key_id) == ids
    with sessions() as db:
        assert db.scalar(select(ApplicationDB).where(ApplicationDB.slug == "uncommitted")) is None
    assert len(calls) == 1


@pytest.mark.parametrize("path,status", [("/api/v1/items/1", 200), ("/api/v1/limited", 429), ("/api/v1/explode", 500)])
def test_audit_database_failure_never_replaces_response(audit_app, caplog, path, status):
    app, sessions, key, _, calls, failure = audit_app
    failure["enabled"] = True
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(path, headers={"X-API-Key": key})
    assert response.status_code == status
    assert rows(sessions) == [] and len(calls) == 1
    assert "Request audit persistence failed" in caplog.text
    assert "sensitive database" not in caplog.text and key not in caplog.text


def test_unmatched_path_is_redacted_and_probes_do_not_open_audit_sessions(audit_app):
    app, sessions, key, _, calls, _ = audit_app
    with TestClient(app) as client:
        assert client.get("/api/v1/health").status_code == 200
        assert client.get("/docs-probe").status_code == 200
        assert client.get("/api/v10/unrelated").status_code == 404
        assert calls == []
        assert client.get(f"/api/v1/unmatched/{key}?token=secret").status_code == 404
    audit, = rows(sessions)
    assert audit.path == "/<unmatched>" and audit.status_code == 404


def test_factory_auditing_is_opt_in():
    app = create_app()
    assert not app.user_middleware


@pytest.mark.asyncio
async def test_streaming_error_preserves_status_already_sent(audit_app):
    _, sessions, _, _, _, _ = audit_app

    async def broken_stream(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("stream failed after headers")

    middleware = RequestAuditMiddleware(broken_stream, sessions)
    sent = []

    async def send(message):
        sent.append(message)

    with pytest.raises(RuntimeError, match="stream failed after headers"):
        await middleware({"type": "http", "path": "/api/v1/stream", "method": "GET"}, AsyncMock(), send)
    audit, = rows(sessions)
    assert audit.status_code == sent[0]["status"] == 200


@pytest.mark.asyncio
async def test_session_creation_failure_does_not_mask_original_exception(caplog):
    async def broken_app(scope, receive, send):
        raise ValueError("original error")

    def unavailable_database():
        raise RuntimeError("sensitive connection details")

    middleware = RequestAuditMiddleware(broken_app, unavailable_database)
    with pytest.raises(ValueError, match="original error"):
        await middleware({"type": "http", "path": "/api/v1/test", "method": "GET"}, AsyncMock(), AsyncMock())
    assert "Request audit persistence failed" in caplog.text
    assert "sensitive connection details" not in caplog.text


def test_audit_middleware_installed_once_in_core_only():
    from app.core_main import app as core_app, audit_sessions
    from app.main import app as legacy_app

    installed = [entry for entry in core_app.user_middleware if entry.cls is RequestAuditMiddleware]
    assert len(installed) == 1
    assert installed[0].kwargs["session_factory"] is audit_sessions
    assert not any(entry.cls is RequestAuditMiddleware for entry in legacy_app.user_middleware)


@pytest.mark.asyncio
async def test_audit_deadline_bounds_wait_for_blocked_driver(caplog):
    entered, release, finished = Event(), Event(), Event()

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            finished.set()

        def add(self, row):
            pass

        def commit(self):
            entered.set()
            release.wait(timeout=2)

    async def endpoint(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = RequestAuditMiddleware(endpoint, FakeSession, timeout_seconds=0.1)
    sent = []

    async def send(message):
        sent.append(message)

    started = perf_counter()
    try:
        await middleware({"type": "http", "path": "/api/v1/test", "method": "GET"}, AsyncMock(), send)
        assert perf_counter() - started < 1
        assert sent[0]["status"] == 200 and sent[1]["body"] == b"ok"
        assert "Request audit persistence failed" in caplog.text
    finally:
        release.set()
        # Join the fake driver's worker before the test completes.
        if entered.is_set():
            assert await anyio.to_thread.run_sync(finished.wait, 2)


@pytest.mark.asyncio
async def test_writer_capacity_drops_without_opening_another_session(caplog):
    factory = AsyncMock()
    async def endpoint(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
    middleware = RequestAuditMiddleware(endpoint, factory)
    assert middleware.workers.acquire(False) and middleware.workers.acquire(False)
    try:
        await middleware({"type": "http", "path": "/api/v1/test", "method": "GET"}, AsyncMock(), AsyncMock())
    finally:
        middleware.workers.release()
        middleware.workers.release()
    factory.assert_not_called()
    assert "Request audit persistence failed" in caplog.text
