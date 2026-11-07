"""Read-only Global Chess startup against Alembic-built disposable PostgreSQL."""

import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import OperationalError

from app import global_chess_readiness as readiness


def migrate(connection):
    config = Config();config.set_main_option("script_location", str(readiness.MIGRATIONS));config.attributes["connection"] = connection
    command.upgrade(config, "head")


def record(engine):
    statements = []
    def capture(conn, cursor, statement, parameters, context, many):statements.append(statement)
    event.listen(engine, "before_cursor_execute", capture)
    return statements, capture


def assert_read_only(statements):
    assert statements and all(re.match(r"\s*(SELECT|SHOW)\b", statement, re.I) for statement in statements)


def test_readiness_is_read_only_and_resets_connection_characteristics(migration_engine):
    with migration_engine.begin() as connection:migrate(connection)
    engine = create_engine(migration_engine.url, pool_size=1, max_overflow=0, connect_args={"connect_timeout": 3})
    with engine.connect() as connection:pid = connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
    statements, capture = record(engine);modes = []
    def mode(conn, cursor, statement, parameters, context, many):
        if statement.strip() == "SELECT current_schema()":
            cursor.execute("SHOW transaction_read_only");modes.append(cursor.fetchone()[0])
    event.listen(engine, "before_cursor_execute", mode)
    try:
        readiness.validate_global_chess_database(engine)
        assert modes == ["on"]
        assert_read_only(statements)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT pg_backend_pid()")).scalar_one() == pid
            assert connection.execute(text("SHOW transaction_read_only")).scalar_one() == "off"
    finally:
        event.remove(engine, "before_cursor_execute", capture);event.remove(engine, "before_cursor_execute", mode)
        engine.dispose()


@pytest.mark.parametrize("state,message", [
    ("empty", "migration history is missing"), ("no_record", "no migration revision record"),
    ("outdated", "expected Global Chess Alembic revision"), ("unknown", "expected Global Chess Alembic revision"),
    ("multiple", "expected Global Chess Alembic revision"), ("missing_table", "missing required Global Chess tables"),
    ("only_core_version", "migration history is missing"),
])
def test_unready_databases_fail_without_schema_or_version_writes(migration_engine, state, message):
    with migration_engine.begin() as connection:
        if state not in {"empty", "only_core_version"}:migrate(connection)
        if state == "no_record":connection.execute(text("DELETE FROM global_chess_alembic_version"))
        elif state in {"outdated", "unknown"}:
            connection.execute(text("UPDATE global_chess_alembic_version SET version_num=:revision"),
                               {"revision": "base" if state == "outdated" else "untrusted-private-revision"})
        elif state == "multiple":connection.execute(text("INSERT INTO global_chess_alembic_version VALUES ('other-private-revision')"))
        elif state == "missing_table":connection.execute(text("DROP TABLE research_sources"))
        elif state == "only_core_version":
            connection.execute(text("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)"))
            connection.execute(text("INSERT INTO alembic_version VALUES ('61c2a9e47b30')"))
        before = set(inspect(connection).get_table_names())
        revisions = connection.execute(text("SELECT version_num FROM global_chess_alembic_version ORDER BY version_num")).scalars().all() if readiness.VERSION_TABLE in before else None
    statements, capture = record(migration_engine)
    try:
        with pytest.raises(readiness.GlobalChessReadinessError, match=message) as error:
            readiness.validate_global_chess_database(migration_engine)
        assert "untrusted-private-revision" not in str(error.value) and "other-private-revision" not in str(error.value)
    finally:event.remove(migration_engine, "before_cursor_execute", capture)
    assert_read_only(statements)
    with migration_engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) == before
        if revisions is not None:
            assert connection.execute(text("SELECT version_num FROM global_chess_alembic_version ORDER BY version_num")).scalars().all() == revisions


def test_wrong_schema_fails_read_only(migration_engine):
    engine = create_engine(migration_engine.url, connect_args={"connect_timeout": 3, "options": "-c search_path=pg_catalog"})
    statements, capture = record(engine)
    try:
        with pytest.raises(readiness.GlobalChessReadinessError, match="expects public"):
            readiness.validate_global_chess_database(engine)
        assert_read_only(statements)
    finally:event.remove(engine, "before_cursor_execute", capture);engine.dispose()


def test_unavailable_database_error_hides_connection_details():
    engine = Mock();engine.dialect.name = "postgresql"
    engine.connect.side_effect = OperationalError("private DSN", {}, Exception("password=private"))
    with pytest.raises(readiness.GlobalChessReadinessError) as error:readiness.validate_global_chess_database(engine)
    assert "GLOBAL_CHESS_DATABASE_URL" in str(error.value) and "private" not in str(error.value)
    assert error.value.__suppress_context__ is True


def test_non_postgres_is_rejected_before_connecting():
    engine = Mock();engine.dialect.name = "sqlite"
    with pytest.raises(readiness.GlobalChessReadinessError, match="requires PostgreSQL"):
        readiness.validate_global_chess_database(engine)
    engine.connect.assert_not_called()


@pytest.mark.parametrize("heads,bases", [([], []), (["one", "two"], ["root"]), (["one"], ["first", "second"])])
def test_invalid_graph_fails_before_database_access(monkeypatch, heads, bases):
    scripts = Mock();scripts.get_heads.return_value = heads;scripts.get_bases.return_value = bases
    monkeypatch.setattr(readiness.ScriptDirectory, "from_config", Mock(return_value=scripts))
    engine = Mock()
    with pytest.raises(readiness.GlobalChessReadinessError, match="one root and one head"):
        readiness.validate_global_chess_database(engine)
    engine.connect.assert_not_called()


@pytest.mark.parametrize("healthy", [True, False])
def test_actual_startup_is_read_only_and_always_disposes_owned_resources(migration_engine, monkeypatch, healthy):
    from app import main
    from app.core import database as core_database
    if healthy:
        with migration_engine.begin() as connection:migrate(connection)
    for name in ("upgrade", "downgrade", "stamp"):
        monkeypatch.setattr(command, name, Mock(side_effect=AssertionError("Startup invoked Alembic mutation")))
    monkeypatch.setattr(main, "engine", migration_engine)
    own_dispose = Mock(wraps=migration_engine.dispose)
    monkeypatch.setattr(migration_engine, "dispose", own_dispose)
    core_dispose = Mock(side_effect=AssertionError("Global Chess touched Core lifecycle"))
    monkeypatch.setattr(core_database.engine, "dispose", core_dispose)
    statements, capture = record(migration_engine)
    try:
        if healthy:
            with TestClient(main.app) as client:assert client.get("/health").status_code == 200
        else:
            with pytest.raises(readiness.GlobalChessReadinessError, match="migration history is missing"):
                with TestClient(main.app):pytest.fail("Uninitialized application served requests")
    finally:event.remove(migration_engine, "before_cursor_execute", capture)
    assert_read_only(statements)
    own_dispose.assert_called_once();core_dispose.assert_not_called()


def test_legacy_fallback_cannot_select_startup_database(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "global_chess_settings", SimpleNamespace(database_url=None))
    check = Mock(side_effect=AssertionError("Startup followed legacy URL"))
    monkeypatch.setattr(main, "validate_global_chess_database", check)
    monkeypatch.setattr(main, "engine", Mock())
    with pytest.raises(readiness.GlobalChessReadinessError, match="requires explicit GLOBAL_CHESS_DATABASE_URL"):
        with TestClient(main.app):pytest.fail("Legacy target was selected")
    check.assert_not_called()
    main.engine.dispose.assert_called_once()
