
from pathlib import Path
import json
from typing import Annotated
from uuid import UUID

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Always load the environment file from backend/.env.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    app_name: str = "Argus-Core"
    app_version: str = "0.1.0"
    environment: str = "development"

    database_url: str | None = None
    redis_url: str = "redis://127.0.0.1:6379/0"

    ollama_base_url: str = "http://localhost:12400"
    ollama_model: str = "qwen2.5:3b"

    # Operator configuration only; keys are authenticated core_applications IDs.
    ai_application_quota_overrides: Annotated[dict[str, int], NoDecode] = Field(default_factory=dict)
    ai_max_concurrent_inferences: int = Field(default=1, ge=1, le=8)
    ai_inference_lease_seconds: int = Field(default=210, ge=200, le=300)

    @field_validator("ai_max_concurrent_inferences", "ai_inference_lease_seconds", mode="before")
    @classmethod
    def validate_ai_integer(cls, value):
        if isinstance(value, str) and value.isascii() and value.isdigit():
            value = int(value)
        if type(value) is not int:
            raise ValueError("AI capacity settings require integers")
        return value

    @field_validator("ai_application_quota_overrides", mode="before")
    @classmethod
    def validate_ai_quotas(cls, value):
        def unique_object(pairs):
            result = {}
            for key, quota in pairs:
                if key in result:
                    raise ValueError("Duplicate AI quota application identifier")
                result[key] = quota
            return result

        if isinstance(value, str):
            try:
                value = json.loads(value, object_pairs_hook=unique_object)
            except (ValueError, TypeError):
                raise ValueError("Invalid AI quota override configuration") from None
        if not isinstance(value, dict) or len(value) > 100:
            raise ValueError("AI quota overrides must be a bounded application-ID mapping")
        for identifier, quota in value.items():
            try:
                valid_id = isinstance(identifier, str) and str(UUID(identifier)) == identifier
            except ValueError:
                valid_id = False
            if not valid_id or type(quota) is not int or not 1 <= quota <= 60:
                raise ValueError("AI quotas require canonical application UUIDs and integer limits of 1-60")
        return value

    # Explicit operator opt-in; never enabled by a discovery request.
    gdelt_enabled: bool = False

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )


settings = Settings()
