"""Global Chess-owned database and integration settings."""

import logging
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


logger = logging.getLogger(__name__)


class GlobalChessDatabaseConfigurationError(RuntimeError):
    """Sanitized database configuration failure; never include a connection URL."""


class GlobalChessSettings(BaseSettings):
    core_base_url: str | None = None
    core_api_key: SecretStr | None = Field(default=None, repr=False)
    core_request_timeout: float = Field(default=190.0, gt=0, le=600, allow_inf_nan=False)
    database_url: SecretStr | None = Field(default=None, repr=False)
    environment: str = "development"
    allow_legacy_database_url: bool = True
    # Temporary compatibility inputs, read independently of app.core.config.
    legacy_database_url: SecretStr | None = Field(default=None, validation_alias="DATABASE_URL", repr=False, exclude=True)
    legacy_environment: str | None = Field(default=None, validation_alias="ENVIRONMENT", repr=False, exclude=True)

    @field_validator("environment", "legacy_environment")
    @classmethod
    def normalized_environment(cls, value):
        return value.strip().casefold() if value is not None else None

    @field_validator("database_url")
    @classmethod
    def explicit_database_is_nonblank(cls, value):
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("GLOBAL_CHESS_DATABASE_URL must not be blank")
        return value

    def database_connection_url(self) -> str:
        if self.database_url is not None:
            return self.database_url.get_secret_value()
        if (self.environment == "development" and self.legacy_environment in {None, "development"}
                and self.allow_legacy_database_url and self.legacy_database_url is not None
                and self.legacy_database_url.get_secret_value().strip()):
            logger.warning(
                "Global Chess is using the temporary development-only DATABASE_URL fallback. "
                "Set GLOBAL_CHESS_DATABASE_URL explicitly; disable compatibility with "
                "GLOBAL_CHESS_ALLOW_LEGACY_DATABASE_URL=false."
            )
            return self.legacy_database_url.get_secret_value()
        raise GlobalChessDatabaseConfigurationError(
            "GLOBAL_CHESS_DATABASE_URL is required. Legacy DATABASE_URL compatibility "
            "is available only in development when enabled; it is disabled outside development."
        )

    model_config = SettingsConfigDict(
        env_prefix="GLOBAL_CHESS_",
        env_file=Path(__file__).resolve().parents[1] / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
        populate_by_name=True,
    )


# Research's approved Core identifiers remain independently owned.
RESEARCH_RSS_FEEDS = (("BBC World", "bbc-world"), ("NPR Politics", "npr-politics"))

# Independent ingestion ownership; keep the legacy labels and traversal order.
INGESTION_RSS_FEEDS = (("BBC World", "bbc-world"), ("NPR Politics", "npr-politics"))

global_chess_settings = GlobalChessSettings()
