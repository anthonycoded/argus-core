
import os

import pytest
from sqlalchemy import create_engine
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
