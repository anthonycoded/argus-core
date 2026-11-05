from datetime import datetime, timedelta, timezone

import pytest
from starlette.requests import Request
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.auth import get_current_application
from app.core.models import CoreBase
from app.core.models.api_key import APIKeyDB
from app.core.models.application import ApplicationDB
from app.services.api_key_service import generate_api_key


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


@pytest.fixture
def registered_key(db):
    application = ApplicationDB(
        name="ARGUS",
        slug="argus",
        is_active=True,
    )
    db.add(application)
    db.commit()
    db.refresh(application)

    generated = generate_api_key()

    api_key = APIKeyDB(
        application_id=application.id,
        name="Test Key",
        key_prefix=generated.key_prefix,
        key_hash=generated.key_hash,
        scopes=["sources:read"],
        is_active=True,
    )

    db.add(api_key)
    db.commit()
    db.refresh(api_key)

    return application, api_key, generated.plaintext_key

@pytest.fixture
def mock_request():
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/v1/test",
        "headers": [],
    })

def test_authenticate_valid_api_key(db, registered_key, mock_request):
    application, api_key, plaintext = registered_key

    result = get_current_application(
        request=mock_request,
        x_api_key=plaintext,
        db=db,
    )

    assert result.application.id == application.id
    assert result.api_key.id == api_key.id
    assert result.scopes == ["sources:read"]


def test_reject_missing_api_key(db):
    with pytest.raises(HTTPException) as exc:
        get_current_application(
            request=mock_request,
            x_api_key=None,
            db=db,
        )

    assert exc.value.status_code == 401


def test_reject_invalid_api_key(db):
    with pytest.raises(HTTPException) as exc:
        get_current_application(
            request=mock_request,
            x_api_key="argus_live_invalid_secret",
            db=db,
        )

    assert exc.value.status_code == 401


def test_reject_revoked_api_key(db, registered_key):
    _, api_key, plaintext = registered_key

    api_key.is_active = False
    db.commit()

    with pytest.raises(HTTPException) as exc:
        get_current_application(
            request=mock_request,
            x_api_key=plaintext,
            db=db,
        )

    assert exc.value.status_code == 401


def test_reject_inactive_application(db, registered_key):
    application, _, plaintext = registered_key

    application.is_active = False
    db.commit()

    with pytest.raises(HTTPException) as exc:
        get_current_application(
            request=mock_request,
            x_api_key=plaintext,
            db=db,
        )

    assert exc.value.status_code == 401


def test_reject_expired_api_key(db, registered_key):
    _, api_key, plaintext = registered_key

    api_key.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()

    with pytest.raises(HTTPException) as exc:
        get_current_application(
            request=mock_request,
            x_api_key=plaintext,
            db=db,
        )

    assert exc.value.status_code == 401


def test_update_last_used_timestamp(db, registered_key, mock_request):
    _, api_key, plaintext = registered_key

    assert api_key.last_used_at is None

    get_current_application(
        request=mock_request,
        x_api_key=plaintext,
        db=db,
    )

    db.refresh(api_key)

    assert api_key.last_used_at is not None