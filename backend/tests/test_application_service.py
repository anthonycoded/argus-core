import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.models import CoreBase
from app.services.application_service import (
    get_application_by_slug,
    register_application,
)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    CoreBase.metadata.create_all(bind=engine)

    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()

    yield session

    session.close()
    CoreBase.metadata.drop_all(bind=engine)
    engine.dispose()


def test_register_application(db):
    application = register_application(
        db,
        name="ARGUS",
        slug="argus",
        description="Geopolitical OSINT monitoring.",
    )

    assert application.id
    assert application.name == "ARGUS"
    assert application.slug == "argus"
    assert application.is_active is True


def test_retrieve_application_by_slug(db):
    register_application(
        db,
        name="ARGUS",
        slug="argus",
    )

    application = get_application_by_slug(db, "argus")

    assert application is not None
    assert application.name == "ARGUS"


def test_unknown_application_returns_none(db):
    assert get_application_by_slug(db, "unknown") is None


def test_duplicate_application_is_rejected(db):
    register_application(
        db,
        name="ARGUS",
        slug="argus",
    )

    with pytest.raises(ValueError):
        register_application(
            db,
            name="Another ARGUS",
            slug="argus",
        )


def test_register_multiple_applications(db):
    argus = register_application(
        db,
        name="ARGUS",
        slug="argus",
    )

    global_chess = register_application(
        db,
        name="Global Chess",
        slug="global-chess",
    )

    assert argus.slug == "argus"
    assert global_chess.slug == "global-chess"
    assert argus.id != global_chess.id