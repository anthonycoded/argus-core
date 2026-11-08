
import os
import importlib.abc
import json
from pathlib import Path
import sys

import pytest
from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core.models import CoreBase


class CoreOnlyImports(importlib.abc.MetaPathFinder):
    """Fail collection/execution if the Core suite loads a legacy consumer."""

    excluded = (
        "app.models", "app.database", "app.main", "app.config",
        "app.global_chess", "app.agents", "app.routes", "app.schemas",
        "app.clients.argus_core_client", "app.clients.classification_contracts",
        "app.clients.discovery_contracts", "app.clients.rss_discovery_contracts",
        "app.clients.source_contracts", "app.clients.global_chess_ollama_client",
        "app.services.classification_coverage", "app.services.ingestion_discovery",
        "app.services.ollama_service", "app.services.research_",
        "app.services.synthesis_service", "app.services.rss_service",
        "app.services.rss_utils", "app.services.topic_scoring",
    )

    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + ".")
               or (name.endswith(("_", "global_chess")) and fullname.startswith(name))
               for name in self.excluded):
            raise AssertionError("Core-only suite imported legacy application code: " + fullname)


def pytest_configure(config):
    # The default suite is Core-only after retiring legacy application tests.
    sys.meta_path.insert(0, CoreOnlyImports())


@pytest.fixture
def unrelated_application_schema():
    """Frozen five-table sentinel, never imported from an application model.

    Keep the historical table/constraint/index shapes so migration snapshots
    continue detecting destructive changes. No ORM defaults or application
    configuration are involved. SQLite API tests need only SERIAL's portable
    INTEGER spelling; PostgreSQL uses the frozen DDL verbatim.
    """
    frozen = json.loads((Path(__file__).parent / "fixtures/unrelated_application_schema.json").read_text())

    class SentinelSchema:
        names = frozenset(frozen)

        def create(self, connection):
            for name in ("topics", "story_clusters", "topic_analyses", "research_sources", "story_cluster_members"):
                ddl = frozen[name]["table"]
                if connection.dialect.name == "sqlite":
                    ddl = ddl.replace("SERIAL NOT NULL", "INTEGER NOT NULL")
                connection.exec_driver_sql(ddl)
                for index in frozen[name]["indexes"]:
                    connection.exec_driver_sql(index)
            metadata = MetaData()
            metadata.reflect(bind=connection, only=sorted(self.names))
            assert set(metadata.tables) == self.names
            return metadata

    return SentinelSchema()


@pytest.fixture(scope="session")
def postgres_test_engine():
    database_url = os.getenv("ARGUS_CORE_TEST_DATABASE_URL")

    if not database_url:
        pytest.skip(
            "Set ARGUS_CORE_TEST_DATABASE_URL to run PostgreSQL integration tests."
        )

    parsed_url = make_url(database_url)

    if parsed_url.database != "argus_core_test":
        pytest.fail(
            "PostgreSQL integration tests must use the argus_core_test database."
        )

    engine = create_engine(
        database_url,
        pool_pre_ping=True,
    )

    CoreBase.metadata.create_all(bind=engine)

    yield engine

    CoreBase.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture
def postgres_test_session_factory(postgres_test_engine):
    return sessionmaker(
        bind=postgres_test_engine,
        autoflush=False,
        autocommit=False,
    )


@pytest.fixture
def migration_engine(monkeypatch):
    import re
    from uuid import uuid4
    from sqlalchemy.pool import NullPool
    from app.core.config import settings

    raw = os.getenv("ARGUS_CORE_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("Set ARGUS_CORE_TEST_DATABASE_URL to the disposable argus_core_test database.")
    control_url = make_url(raw)
    if control_url.get_backend_name() != "postgresql" or control_url.database != "argus_core_test":
        pytest.fail("Migration tests require the disposable argus_core_test control database.")
    control = create_engine(control_url, isolation_level="AUTOCOMMIT", poolclass=NullPool,
                            connect_args={"connect_timeout": 3})
    name = "argus_core_migration_test_" + uuid4().hex
    assert re.fullmatch(r"argus_core_migration_test_[0-9a-f]{32}", name)
    created = False
    target = None
    try:
        with control.connect() as admin:
            assert admin.execute(text("SELECT current_database()")).scalar() == "argus_core_test"
            if not admin.execute(text("SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname=current_user")).scalar():
                pytest.fail("Disposable migration database provisioning requires CREATEDB; refusing fallback.")
            # The identifier is generated locally, never supplied by environment.
            admin.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0')
            created = True
        target = create_engine(control_url.set(database=name), poolclass=NullPool,
                               connect_args={"connect_timeout": 3})
        with target.connect() as connection:
            assert connection.execute(text("SELECT current_database()")).scalar() == name
            assert inspect(connection).get_table_names(schema="public") == []
        # Any accidental fallback must fail locally, never reach studio-ai.
        monkeypatch.setattr(settings, "database_url", "postgresql+psycopg://127.0.0.1:1/forbidden")
        def forbidden_create_all(*args, **kwargs):
            pytest.fail("Migration tests must initialize Core entirely through Alembic.")
        monkeypatch.setattr(MetaData, "create_all", forbidden_create_all)
        yield target
    finally:
        if target is not None:
            target.dispose()
        if created:
            with control.connect() as admin:
                assert admin.execute(text("SELECT current_database()")).scalar() == "argus_core_test"
                # No FORCE: leaked connections must fail cleanup visibly.
                admin.exec_driver_sql(f'DROP DATABASE "{name}"')
                assert not admin.execute(text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": name}).scalar()
        control.dispose()
