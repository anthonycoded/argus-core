
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.router import api_router
from app.core.database import get_db
from app.core.models import CoreBase, SourceRecordDB
from app.factory import create_app


@pytest.fixture
def test_session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    CoreBase.metadata.create_all(bind=engine)

    TestingSessionLocal = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
    )

    yield TestingSessionLocal

    engine.dispose()


@pytest.fixture
def client(test_session_factory):
    test_app = create_app(
        title="Argus-Core Test",
        version="test",
        routers=(api_router,),
    )

    def override_get_db():
        db = test_session_factory()
        try:
            yield db
        finally:
            db.close()

    test_app.dependency_overrides[get_db] = override_get_db

    with TestClient(test_app) as test_client:
        yield test_client


@pytest.fixture
def seeded_sources(test_session_factory):
    sources = [
        {
            "title": "Argus Ukraine update",
            "url": "https://example.com/ukraine",
            "canonical_url": "https://example.com/ukraine",
            "domain": "example.com",
            "publisher": "Test Publisher",
            "source_type": "news",
            "created_at": datetime(2026, 11, 1, tzinfo=timezone.utc),
        },
        {
            "title": "Argus satellite imagery",
            "url": "https://example.com/satellite",
            "canonical_url": "https://example.com/satellite",
            "domain": "example.com",
            "publisher": "Test Publisher",
            "source_type": "news",
            "created_at": datetime(2026, 11, 2, tzinfo=timezone.utc),
        },
        {
            "title": "Global Chess analysis",
            "url": "https://example.org/analysis",
            "canonical_url": "https://example.org/analysis",
            "domain": "example.org",
            "publisher": "Other Publisher",
            "source_type": "research",
            "created_at": datetime(2026, 11, 3, tzinfo=timezone.utc),
        },
        {
            "title": "Argus Iran update",
            "url": "https://example.com/iran",
            "canonical_url": "https://example.com/iran",
            "domain": "example.com",
            "publisher": "Other Publisher",
            "source_type": "news",
            "created_at": datetime(2026, 11, 4, tzinfo=timezone.utc),
        },
    ]

    with test_session_factory() as db:
        for source_data in sources:
            db.add(SourceRecordDB(**source_data))

        db.commit()

    return sources


def test_list_sources_total_before_pagination(client, seeded_sources):
    response = client.get(
        "/api/v1/sources?publisher=Test%20Publisher&limit=1&offset=1"
    )

    assert response.status_code == 200

    data = response.json()

    assert data["total"] == 2
    assert data["count"] == 1
    assert len(data["items"]) == 1


def test_list_sources_pagination_returns_correct_records(client, seeded_sources):
    response = client.get("/api/v1/sources?limit=2&offset=1")

    assert response.status_code == 200

    data = response.json()

    assert data["total"] == 4
    assert data["count"] == 2
    assert len(data["items"]) == 2

    assert data["items"][0]["title"] == "Global Chess analysis"
    assert data["items"][1]["title"] == "Argus satellite imagery"


def test_list_sources_combined_filters(client, seeded_sources):
    response = client.get(
        "/api/v1/sources"
        "?publisher=Test%20Publisher"
        "&source_type=news"
        "&domain=example.com"
        "&search=Argus"
    )

    assert response.status_code == 200

    data = response.json()

    assert data["total"] == 2
    assert data["count"] == 2

    titles = {source["title"] for source in data["items"]}

    assert titles == {
        "Argus Ukraine update",
        "Argus satellite imagery",
    }

