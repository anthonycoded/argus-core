from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core.auth import AuthenticatedApplication
from app.core.rate_limit import require_rate_limit
from app.services.rate_limiter import RateLimit, RateLimitDecision


@pytest.fixture
def test_auth():
    return AuthenticatedApplication(
        application=SimpleNamespace(id="test-application"),
        api_key=SimpleNamespace(id="test-api-key"),
    )


def create_test_app(test_auth, decision):
    app = FastAPI()

    async def mock_enforce_rate_limit(**kwargs):
        return decision

    from app.core import rate_limit as rate_limit_module

    rate_limit_module.enforce_rate_limit = mock_enforce_rate_limit

    async def override_auth():
        return test_auth

    app.dependency_overrides[
        rate_limit_module.get_current_application
    ] = override_auth

    app.get("/test")(
        lambda auth=Depends(
            require_rate_limit(
                bucket="test",
                rate_limit=RateLimit(
                    requests=5,
                    window_seconds=60,
                ),
            )
        ): {"status": "ok"}
    )

    return app


def test_rate_limit_allows_request(test_auth):
    decision = RateLimitDecision(
        allowed=True,
        limit=5,
        remaining=4,
        retry_after=0,
    )

    app = create_test_app(test_auth, decision)

    with TestClient(app) as client:
        response = client.get("/test")

    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"] == "5"
    assert response.headers["X-RateLimit-Remaining"] == "4"


def test_rate_limit_returns_429_when_exceeded(test_auth):
    decision = RateLimitDecision(
        allowed=False,
        limit=5,
        remaining=0,
        retry_after=30,
    )

    app = create_test_app(test_auth, decision)

    with TestClient(app) as client:
        response = client.get("/test")

    assert response.status_code == 429
    assert response.headers["X-RateLimit-Limit"] == "5"
    assert response.headers["X-RateLimit-Remaining"] == "0"
    assert response.headers["Retry-After"] == "30"
    assert response.json()["detail"] == (
        "Rate limit exceeded. Please try again later."
    )