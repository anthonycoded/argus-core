from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api.v1.router import api_router
from app.core.auth import AuthenticatedApplication, get_current_application
from app.core.rate_limit import require_rate_limit
from app.factory import create_app
from app.services.rate_limiter import (
    GENERAL_API_LIMIT,
    AI_GENERATION_LIMIT,
)


@pytest.fixture
def client():
    test_app = create_app(
        title="Argus-Core Test",
        version="test",
        routers=(api_router,),
    )

    test_auth = AuthenticatedApplication(
        application=SimpleNamespace(
            id="test-application",
            slug="test-application",
        ),
        api_key=SimpleNamespace(
            id="test-api-key",
            scopes=["ai:generate"],
        ),
    )

    test_app.dependency_overrides[get_current_application] = (
        lambda: test_auth
    )

    test_app.dependency_overrides[
        require_rate_limit(
            bucket="general",
            rate_limit=GENERAL_API_LIMIT,
        )
    ] = lambda: test_auth

    test_app.dependency_overrides[
        require_rate_limit(
            bucket="ai_generation",
            rate_limit=AI_GENERATION_LIMIT,
        )
    ] = lambda: test_auth

    with TestClient(test_app) as test_client:
        yield test_client


def test_ai_generation_success(client):
    ollama_response = {
        "model": "qwen2.5:3b",
        "message": {
            "content": '{"summary": "Test geopolitical analysis"}'
        },
    }

    with patch(
        "app.api.v1.ai.ollama_client.generate_structured",
        new_callable=AsyncMock,
        return_value=ollama_response,
    ) as mock_generate:
        response = client.post(
            "/api/v1/ai/generate",
            json={
                "system_prompt": "You are an intelligence analyst.",
                "user_prompt": "Analyze this fictional event.",
                "response_schema": {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"}
                    },
                },
            },
        )

    assert response.status_code == 200

    data = response.json()

    assert data["status"] == "success"
    assert data["model"] == "qwen2.5:3b"
    assert data["result"] == '{"summary": "Test geopolitical analysis"}'

    mock_generate.assert_awaited_once()


def test_ai_generation_rejects_empty_prompts(client):
    response = client.post(
        "/api/v1/ai/generate",
        json={
            "system_prompt": "",
            "user_prompt": "Analyze this event.",
            "response_schema": {"type": "object"},
        },
    )

    assert response.status_code == 422