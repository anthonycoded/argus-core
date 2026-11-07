"""Startup checks against real, Alembic-built disposable PostgreSQL databases."""

import re
from unittest.mock import AsyncMock, Mock

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import OperationalError

from app.core import readiness
from app.factory import create_app


def migrate(connection, revision="head"):
    cfg = Config()
    cfg.set_main_option("script_location", str(readiness.MIGRATIONS))
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, revision)


def record_statements(engine):
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    return statements, capture


def assert_only_reads(statements):
    assert statements
    assert all(re.match(r"\s*(SELECT|SHOW)\b", statement, re.I) for statement in statements)


def test_healthy_database_check_is_readonly_and_resets_pool_state(migration_engine):
    with migration_engine.begin() as connection:
        migrate(connection)
    # Reuse one physical connection, rather than letting NullPool hide leaked
    # READ ONLY characteristics on a newly opened connection.
    pooled = create_engine(migration_engine.url, pool_size=1, max_overflow=0,
                           connect_args={"connect_timeout": 3})
    with pooled.connect() as connection:
        original_pid = connection.execute(text("SELECT pg_backend_pid()")).scalar()
    statements, capture = record_statements(pooled)
    checked_modes = []
    def check_mode(conn, cursor, statement, parameters, context, executemany):
        if statement.strip() == "SELECT current_schema()":
            # Use the cursor directly to avoid recursive SQLAlchemy events.
            cursor.execute("SHOW transaction_read_only")
            checked_modes.append(cursor.fetchone()[0])
    event.listen(pooled, "before_cursor_execute", check_mode)
    try:
        readiness.validate_database_readiness(pooled)
        assert checked_modes == ["on"]
        assert_only_reads(statements)
        with pooled.connect() as connection:
            assert connection.execute(text("SELECT pg_backend_pid()")).scalar() == original_pid
            assert connection.execute(text("SHOW transaction_read_only")).scalar() == "off"
    finally:
        event.remove(pooled, "before_cursor_execute", capture)
        event.remove(pooled, "before_cursor_execute", check_mode)
        pooled.dispose()


def test_different_current_schema_is_rejected(migration_engine):
    alternate = create_engine(migration_engine.url, connect_args={
        "connect_timeout": 3, "options": "-c search_path=pg_catalog",
    })
    try:
        with pytest.raises(readiness.DatabaseReadinessError, match="expects public"):
            readiness.validate_database_readiness(alternate)
    finally:
        alternate.dispose()


@pytest.mark.parametrize("state,message", [
    ("empty", "migration history is missing"),
    ("no_revision", "no migration revision record"),
    ("outdated", "expected Argus-Core Alembic revision"),
    ("unknown", "expected Argus-Core Alembic revision"),
    ("multiple", "expected Argus-Core Alembic revision"),
    ("missing_table", "missing required Core tables: core_audit_logs"),
])
def test_startup_rejects_unready_database_without_changing_it(migration_engine, state, message):
    with migration_engine.begin() as connection:
        if state != "empty":
            migrate(connection, "8d3f39d3a946" if state == "outdated" else "head")
        if state == "no_revision":
            connection.execute(text("DELETE FROM alembic_version"))
        elif state == "unknown":
            connection.execute(text("UPDATE alembic_version SET version_num='unknown-fixture-revision'"))
        elif state == "multiple":
            connection.execute(text("INSERT INTO alembic_version(version_num) VALUES ('other-fixture-revision')"))
        elif state == "missing_table":
            connection.execute(text("DROP TABLE core_audit_logs"))
        before = set(inspect(connection).get_table_names())
        old_revisions = connection.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num")).scalars().all() if "alembic_version" in before else None
    statements, capture = record_statements(migration_engine)
    try:
        with pytest.raises(readiness.DatabaseReadinessError, match=message):
            readiness.validate_database_readiness(migration_engine)
    finally:
        event.remove(migration_engine, "before_cursor_execute", capture)
    assert_only_reads(statements)
    with migration_engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) == before
        if old_revisions is not None:
            assert connection.execute(text("SELECT version_num FROM alembic_version ORDER BY version_num")).scalars().all() == old_revisions


def test_unavailable_database_error_is_actionable_and_sanitized():
    engine = Mock()
    engine.dialect.name = "postgresql"
    engine.connect.side_effect = OperationalError("secret DSN", {}, Exception("password=private"))
    with pytest.raises(readiness.DatabaseReadinessError) as error:
        readiness.validate_database_readiness(engine)
    assert "PostgreSQL availability" in str(error.value)
    assert "DATABASE_URL" in str(error.value)
    assert "private" not in str(error.value) and "secret" not in str(error.value)
    assert error.value.__suppress_context__ is True


def test_non_postgres_is_rejected_without_connecting():
    engine = Mock()
    engine.dialect.name = "sqlite"
    with pytest.raises(readiness.DatabaseReadinessError, match="requires a PostgreSQL"):
        readiness.validate_database_readiness(engine)
    engine.connect.assert_not_called()


@pytest.mark.parametrize("heads", [[], ["one", "two"]])
def test_invalid_deployed_graph_is_rejected_before_connecting(monkeypatch, heads):
    scripts = Mock()
    scripts.get_heads.return_value = heads
    monkeypatch.setattr(readiness.ScriptDirectory, "from_config", Mock(return_value=scripts))
    engine = Mock()
    with pytest.raises(readiness.DatabaseReadinessError, match="exactly one head"):
        readiness.validate_database_readiness(engine)
    engine.connect.assert_not_called()


@pytest.mark.parametrize("healthy", [True, False])
def test_actual_core_lifespan_validates_and_always_shuts_down(migration_engine, monkeypatch, healthy):
    from app import core_main
    from app.core import database

    if healthy:
        with migration_engine.begin() as connection:
            migrate(connection)
    monkeypatch.setattr(database, "engine", migration_engine)
    close_redis = AsyncMock()
    dispose_audit = Mock()
    monkeypatch.setattr(core_main, "close_redis", close_redis)
    monkeypatch.setattr(core_main.audit_sessions.kw["bind"], "dispose", dispose_audit)
    if healthy:
        with TestClient(core_main.app) as client:
            response = client.get("/api/v1/health")
            assert response.status_code == 200
    else:
        with pytest.raises(readiness.DatabaseReadinessError, match="migration history is missing"):
            with TestClient(core_main.app):
                pytest.fail("Unready Core app must not serve requests")
    close_redis.assert_awaited_once()
    dispose_audit.assert_called_once()


def test_global_chess_startup_no_longer_creates_schema(monkeypatch):
    from app import main
    from app.database import Base

    check = Mock()
    monkeypatch.setattr(main, "validate_global_chess_database", check)
    forbidden = Mock(side_effect=AssertionError("Global Chess startup attempted schema creation"))
    monkeypatch.setattr(Base.metadata, "create_all", forbidden)
    with TestClient(main.app):
        pass
    check.assert_called_once_with(main.engine)
    forbidden.assert_not_called()


def test_async_readiness_failure_keeps_factory_shutdown_behavior():
    check = AsyncMock(side_effect=readiness.DatabaseReadinessError("not ready"))
    shutdown = AsyncMock()
    app = create_app(check_database=check, on_shutdown=shutdown)
    with pytest.raises(readiness.DatabaseReadinessError, match="not ready"):
        with TestClient(app):
            pytest.fail("Startup should fail")
    check.assert_awaited_once()
    shutdown.assert_awaited_once()
