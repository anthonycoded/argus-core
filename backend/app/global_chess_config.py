"""Global Chess-owned integration settings, independent of Core/Ollama settings."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class GlobalChessSettings(BaseSettings):
    core_base_url: str | None = None
    core_api_key: SecretStr | None = Field(default=None, repr=False)
    core_request_timeout: float = Field(default=190.0, gt=0, le=600, allow_inf_nan=False)

    model_config = SettingsConfigDict(
        env_prefix="GLOBAL_CHESS_",
        env_file=Path(__file__).resolve().parents[1] / ".env",
        extra="ignore",
        hide_input_in_errors=True,
    )


global_chess_settings = GlobalChessSettings()
