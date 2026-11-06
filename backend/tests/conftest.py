
import os

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.core.models import CoreBase


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
    from app.core.database import Base

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
        monkeypatch.setattr(CoreBase.metadata, "create_all", forbidden_create_all)
        monkeypatch.setattr(Base.metadata, "create_all", forbidden_create_all)
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
