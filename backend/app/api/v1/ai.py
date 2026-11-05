import logging
from typing import Any
import httpx
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, Field

from app.clients.ollama_client import OllamaClient
from app.core.permissions import require_scopes

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


@router.post("/generate")
async def generate_ai_response(
    request: AIRequest,
    _auth=Depends(require_scopes("ai:generate")),
):
    try:
        payload = await ollama_client.generate_structured(
            system_prompt=request.system_prompt,
            user_prompt=request.user_prompt,
            response_schema=request.response_schema,
            timeout=request.timeout,
            temperature=request.temperature,
        )

        content = payload["message"]["content"]

        return {
            "status": "success",
            "model": payload.get("model"),
            "result": content,
        }

    except httpx.TimeoutException as exc:
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