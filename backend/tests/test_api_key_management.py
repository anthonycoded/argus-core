import pytest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.models import CoreBase
from app.core.models.api_key import APIKeyDB
from app.core.models.application import ApplicationDB
from app.services.api_key_management import (
    get_api_key_by_prefix,
    issue_api_key,
    revoke_api_key,
)
from app.services.api_key_service import verify_api_key


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

    application = ApplicationDB(
        name="ARGUS",
        slug="argus",
        description="Geopolitical OSINT monitoring.",
    )
    session.add(application)
    session.commit()

    yield session

    session.close()
    CoreBase.metadata.drop_all(bind=engine)
    engine.dispose()


def test_issue_api_key(db):
    result = issue_api_key(
        db,
        application_slug="argus",
        name="ARGUS Development",
        scopes=["sources:read", "sources:write"],
    )

    assert result["api_key"].startswith("argus_live_")
    assert result["application_slug"] == "argus"
    assert result["scopes"] == ["sources:read", "sources:write"]
    assert result["id"]


def test_api_key_hash_is_persisted_not_plaintext(db):
    result = issue_api_key(
        db,
        application_slug="argus",
        name="ARGUS Development",
        scopes=["sources:read"],
    )

    record = db.scalar(
        select(APIKeyDB).where(APIKeyDB.id == result["id"])
    )

    assert record is not None
    assert record.key_hash != result["api_key"]
    assert verify_api_key(result["api_key"], record.key_hash)
    assert record.key_prefix == result["api_key"][:16]


def test_get_api_key_by_prefix(db):
    result = issue_api_key(
        db,
        application_slug="argus",
        name="ARGUS Development",
        scopes=["sources:read"],
    )

    record = get_api_key_by_prefix(
        db,
        result["api_key"][:16],
    )

    assert record is not None
    assert record.id == result["id"]


def test_revoke_api_key(db):
    result = issue_api_key(
        db,
        application_slug="argus",
        name="ARGUS Development",
        scopes=["sources:read"],
    )

    assert revoke_api_key(db, key_id=result["id"]) is True

    record = db.get(APIKeyDB, result["id"])

    assert record is not None
    assert record.is_active is False


def test_revoke_unknown_api_key(db):
    assert revoke_api_key(
        db,
        key_id="unknown-key-id",
    ) is False


def test_issue_key_for_unknown_application(db):
    with pytest.raises(ValueError):
        issue_api_key(
            db,
            application_slug="unknown",
            name="Unknown Application",
            scopes=["sources:read"],
        )


def test_issue_key_for_inactive_application(db):
    application = db.scalar(
        select(ApplicationDB).where(
            ApplicationDB.slug == "argus"
        )
    )

    application.is_active = False
    db.commit()

    with pytest.raises(ValueError):
        issue_api_key(
            db,
            application_slug="argus",
            name="ARGUS Development",
            scopes=["sources:read"],
        )