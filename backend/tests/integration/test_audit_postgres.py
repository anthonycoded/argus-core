"""Schema and timeout checks restricted to the disposable PostgreSQL fixture."""

import importlib.util
from pathlib import Path
from time import perf_counter

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import DBAPIError, TimeoutError as PoolTimeout

from app.core.audit import RequestAuditMiddleware, create_audit_session_factory
from app.core.models import APIKeyDB, ApplicationDB, AuditLogDB, CoreBase
from app.core.database import get_db
from app.core.permissions import require_scopes
from app.factory import create_app
from app.services.api_key_service import generate_api_key


def test_postgres_audit_migration_matches_model(postgres_test_engine):
    engine = postgres_test_engine
    with engine.begin() as connection:
        assert connection.execute(text("SELECT current_database()")).scalar() == "argus_core_test"
        # Replace only this fixture-created table with the actual existing
        # migration, then compare the real PostgreSQL schema with ORM metadata.
        AuditLogDB.__table__.drop(connection)
        path = Path(__file__).parents[2] / "migrations/versions/dea3e0498bf6_add_audit_logs.py"
        spec = importlib.util.spec_from_file_location("audit_migration_test", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, CoreBase.metadata) == []
        columns = {column["name"]: column for column in inspect(connection).get_columns("core_audit_logs")}
        assert columns["duration_ms"]["nullable"] is False
        assert columns["duration_ms"]["type"].python_type is int
        assert columns["created_at"]["type"].timezone is True


@pytest.fixture
def audit_sessions(postgres_test_engine):
    sessions = create_audit_session_factory(postgres_test_engine.url)
    yield sessions
    sessions.kw["bind"].dispose()


def test_postgres_audit_timeout_settings_and_statement_bound(audit_sessions):
    engine = audit_sessions.kw["bind"]
    with engine.connect() as connection:
        assert connection.execute(text("SHOW statement_timeout")).scalar() == "1s"
        assert connection.execute(text("SHOW lock_timeout")).scalar() == "500ms"
        assert connection.execute(text("SHOW idle_in_transaction_session_timeout")).scalar() == "2s"
        started = perf_counter()
        with pytest.raises(DBAPIError) as error:
            connection.execute(text("SELECT pg_sleep(10)"))
        assert error.value.orig.sqlstate == "57014"
        assert perf_counter() - started < 5


def test_postgres_audit_pool_checkout_is_bounded(audit_sessions):
    engine = audit_sessions.kw["bind"]
    middleware = RequestAuditMiddleware(None, audit_sessions)
    with engine.connect(), engine.connect():
        started = perf_counter()
        with pytest.raises(PoolTimeout):
            middleware.persist({"method": "GET", "path": "/api/v1/test", "status_code": 200, "duration_ms": 1})
        assert perf_counter() - started < 5


def test_postgres_audit_lock_timeout_preserves_api_response(postgres_test_engine, audit_sessions, caplog):
    router = APIRouter()
    @router.get("/api/v1/audit-lock-test")
    def endpoint():
        return {"status": "ok"}
    app = create_app(routers=(router,), audit_session_factory=audit_sessions)
    with postgres_test_engine.connect() as blocker:
        blocker.execute(text("LOCK TABLE core_audit_logs IN ACCESS EXCLUSIVE MODE"))
        try:
            started = perf_counter()
            with TestClient(app) as client:
                response = client.get("/api/v1/audit-lock-test")
            assert perf_counter() - started < 5
            assert response.status_code == 200 and response.json() == {"status": "ok"}
            assert "Request audit persistence failed" in caplog.text
        finally:
            blocker.rollback()
    with postgres_test_engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM core_audit_logs")).scalar() == 0


def test_postgres_endpoint_rollback_preserves_authenticated_audit(postgres_test_engine, audit_sessions):
    sessions = sessionmaker(bind=postgres_test_engine)
    generated = generate_api_key()
    with sessions() as db:
        application = ApplicationDB(name="PostgreSQL audit test", slug="postgres-audit-test")
        db.add(application)
        db.flush()
        key = APIKeyDB(application_id=application.id, name="Audit test",
                       key_prefix=generated.key_prefix, key_hash=generated.key_hash,
                       scopes=["sources:read"])
        db.add(key)
        db.commit()
        ids = application.id, key.id

    def request_session():
        with sessions() as db:
            yield db

    router = APIRouter()
    @router.get("/api/v1/rollback-test")
    def endpoint(auth=Depends(require_scopes("sources:read")), db=Depends(get_db)):
        db.add(ApplicationDB(name="Never committed", slug="postgres-audit-rollback"))
        db.flush()
        raise RuntimeError("original PostgreSQL endpoint failure")

    app = create_app(routers=(router,), audit_session_factory=audit_sessions)
    app.dependency_overrides[get_db] = request_session
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/rollback-test", headers={"X-API-Key": generated.plaintext_key})
    assert response.status_code == 500 and response.text == "Internal Server Error"
    with sessions() as db:
        assert db.scalar(select(ApplicationDB).where(ApplicationDB.slug == "postgres-audit-rollback")) is None
        row = db.scalar(select(AuditLogDB).where(AuditLogDB.path == "/api/v1/rollback-test"))
        assert row is not None
        assert (row.application_id, row.api_key_id) == ids
        assert row.status_code == 500 and row.duration_ms >= 0
