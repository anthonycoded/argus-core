import logging
from typing import Any
import httpx
from fastapi import APIRouter, HTTPException, Depends, Response
from pydantic import BaseModel, Field

from app.clients.ollama_client import OllamaClient
from app.core.permissions import require_scopes
from app.core.rate_limit import require_rate_limit
from app.core.auth import AuthenticatedApplication, get_current_application
from app.core.config import settings
from app.core.redis import redis_client
from app.services.ai_admission import AIAdmissionError, RedisAIAdmission, run_admitted_inference
from app.services.rate_limiter import (
    GENERAL_API_LIMIT,
    AI_GENERATION_LIMIT,
    RateLimit,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/ai",
    tags=["AI Inference"],
)

ollama_client = OllamaClient()


class AIRequest(BaseModel):
    system_prompt: str = Field(min_length=1, max_length=10000)
    user_prompt: str = Field(min_length=1, max_length=30000)
    response_schema: dict[str, Any]
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    timeout: float = Field(default=60.0, gt=0.0, le=180.0)


async def require_ai_rate_limit(
    response: Response,
    auth: AuthenticatedApplication = Depends(get_current_application),
):
    quota = settings.ai_application_quota_overrides.get(auth.application.id, AI_GENERATION_LIMIT.requests)
    limit = RateLimit(requests=quota, window_seconds=AI_GENERATION_LIMIT.window_seconds)
    return await require_rate_limit(bucket="ai_generation", rate_limit=limit)(response, auth)


def get_ai_admission() -> RedisAIAdmission:
    return RedisAIAdmission(redis_client, capacity=settings.ai_max_concurrent_inferences,
                            lease_seconds=settings.ai_inference_lease_seconds)


@router.post("/generate")
async def generate_ai_response(
    request: AIRequest,
    _auth=Depends(require_scopes("ai:generate")),
    _general_limit=Depends(
        require_rate_limit(
            bucket="general",
            rate_limit=GENERAL_API_LIMIT,
        )
    ),
    _ai_limit=Depends(require_ai_rate_limit),
    admission=Depends(get_ai_admission),
):
    try:
        payload = await run_admitted_inference(admission, lambda: ollama_client.generate_structured(
            system_prompt=request.system_prompt,
            user_prompt=request.user_prompt,
            response_schema=request.response_schema,
            timeout=request.timeout,
            temperature=request.temperature,
        ), timeout=request.timeout)

        content = payload["message"]["content"]

        return {
            "status": "success",
            "model": payload.get("model"),
            "result": content,
        }

    except AIAdmissionError as exc:
        raise HTTPException(status_code=503, detail=str(exc),
                            headers={"Retry-After": str(exc.retry_after)}) from None

    except (httpx.TimeoutException, TimeoutError) as exc:
        raise HTTPException(
            status_code=504,
            detail="AI inference request timed out.",
        ) from exc

    except httpx.HTTPError as exc:
        logger.warning("Ollama inference request failed: %s", exc)

        raise HTTPException(
            status_code=502,
            detail="AI inference service unavailable.",
        ) from exc

    except (KeyError, TypeError, ValueError) as exc:
        logger.exception("Invalid Ollama response")

        raise HTTPException(
            status_code=502,
            detail="AI inference returned an invalid response.",
        ) from exc
