"""Temporary application-owned direct classifier client; no Core configuration.

This preserves the pre-6C /api/chat behavior. Migrating classification to Core
HTTP, changing retries/proxies, or changing editorial prompts is a later phase.
"""

from typing import Any

import httpx

from app.global_chess_config import global_chess_settings


class OllamaClient:
    def __init__(self, base_url: str | None = None, model: str | None = None):
        self.base_url = (base_url or global_chess_settings.ollama_base_url.get_secret_value()).rstrip("/")
        self.model = model or global_chess_settings.ollama_model

    async def generate_structured(
        self, *, system_prompt: str, user_prompt: str,
        response_schema: dict[str, Any], timeout: float = 60.0,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model, "stream": False, "format": response_schema,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "options": {"temperature": temperature},
                },
            )
            response.raise_for_status()
        return response.json()
