
import pytest
from types import SimpleNamespace
from fastapi.testclient import TestClient

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.router import api_router

from app.core.database import get_db
from app.core.models import CoreBase
from app.core.auth import AuthenticatedApplication, get_current_application
from app.core.rate_limit import require_rate_limit

from app.services.rate_limiter import (
    GENERAL_API_LIMIT,
    SOURCE_WRITE_LIMIT,
)

from app.factory import create_app

test_auth = AuthenticatedApplication(
    application=SimpleNamespace(
        id="test-application",
        slug="test-application",
    ),
    api_key=SimpleNamespace(
        id="test-api-key",
        scopes=["sources:read", "sources:write"],
    ),
)

@pytest.fixture
def client():
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

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    test_app = create_app(
        title="Argus-Core Test",
        version="test",
        routers=(api_router,),
    )

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_current_application] = lambda: test_auth
    test_app.dependency_overrides[
        require_rate_limit(
            bucket="general",
            rate_limit=GENERAL_API_LIMIT,
        )
    ] = lambda: test_auth

    test_app.dependency_overrides[
        require_rate_limit(
            bucket="source_write",
            rate_limit=SOURCE_WRITE_LIMIT,
        )
    ] = lambda: test_auth

    with TestClient(test_app) as test_client:
        yield test_client

    engine.dispose()


def test_batch_register_sources(client):
    response = client.post(
        "/api/v1/sources/register/batch",
        json={
            "sources": [
                {
                    "title": "Ukraine update",
                    "url": "https://example.com/ukraine",
                    "content": "Ukraine article content.",
                },
                {
                    "title": "Iran update",
                    "url": "https://example.com/iran",
                    "content": "Iran article content.",
                },
            ]
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["status"] == "completed"
    assert data["total"] == 2
    assert data["created"] == 2
    assert data["duplicates"] == 0
    assert data["failed"] == 0


def test_batch_register_detects_duplicates(client):
    response = client.post(
        "/api/v1/sources/register/batch",
        json={
            "sources": [
                {
                    "title": "First article",
                    "url": "https://example.com/article",
                    "content": "Original article content.",
                },
                {
                    "title": "Duplicate URL",
                    "url": "https://example.com/article?utm_source=test",
                    "content": "Different content.",
                },
                {
                    "title": "Duplicate content",
                    "url": "https://another-example.com/article",
                    "content": "Original article content.",
                },
            ]
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["total"] == 3
    assert data["created"] == 1
    assert data["duplicates"] == 2
    assert data["failed"] == 0

    assert data["results"][1]["reason"] == "canonical_url"
    assert data["results"][2]["reason"] == "content_hash"


@pytest.mark.parametrize(
    "sources",
    [
        [],
        [{"title": "Missing URL"}],
    ],
)
def test_batch_register_rejects_invalid_request(client, sources):
    response = client.post(
        "/api/v1/sources/register/batch",
        json={"sources": sources},
    )

    assert response.status_code == 422


def test_batch_register_rejects_more_than_100_sources(client):
    sources = [
        {
            "title": f"Article {i}",
            "url": f"https://example.com/article-{i}",
        }
        for i in range(101)
    ]

    response = client.post(
        "/api/v1/sources/register/batch",
        json={"sources": sources},
    )

    assert response.status_code == 422

