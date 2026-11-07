
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

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

    # Explicit operator opt-in; never enabled by a discovery request.
    gdelt_enabled: bool = False

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
