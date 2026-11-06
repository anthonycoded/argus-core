"""Global Chess research validation around the authenticated Core HTTP client."""

from typing import TypeVar

from pydantic import BaseModel

from app.clients.argus_core_client import (
    CoreClientError, CoreInferenceClient, get_core_inference_client,
)

ResearchOutput = TypeVar("ResearchOutput", bound=BaseModel)

# Mirrors Core's existing AIRequest contract; no Core implementation imports.
SYSTEM_PROMPT_LIMIT = 10000
USER_PROMPT_LIMIT = 30000


async def generate_research_output(
    *, system_prompt: str, user_prompt: str,
    response_model: type[ResearchOutput], timeout: float, temperature: float,
    inference_client: CoreInferenceClient | None = None,
) -> ResearchOutput:
    for name, prompt, limit in (
        ("system", system_prompt, SYSTEM_PROMPT_LIMIT),
        ("user", user_prompt, USER_PROMPT_LIMIT),
    ):
        if not 1 <= len(prompt) <= limit:
            raise CoreClientError(
                422,
                f"Research {name} prompt must contain 1-{limit} characters "
                "to fit Argus-Core input limits. No inference was requested.",
            )
    generated = await (inference_client or get_core_inference_client()).generate_structured(
        system_prompt=system_prompt, user_prompt=user_prompt,
        response_schema=response_model.model_json_schema(),
        timeout=timeout, temperature=temperature,
    )
    # Keep the original Pydantic validation and public research response shape.
    # These research schemas have no model-provenance field.
    return response_model.model_validate_json(generated.content)
