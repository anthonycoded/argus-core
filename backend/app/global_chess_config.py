"""Global Chess-owned application, editorial, database and integration settings."""

import logging
import math
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


logger = logging.getLogger(__name__)

DEFAULT_RSS_FEEDS = (("BBC World", "bbc-world"), ("NPR Politics", "npr-politics"))
APPROVED_FEED_URLS = {
    "bbc-world": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "npr-politics": "https://feeds.npr.org/1014/rss.xml",
}
DEFAULT_KEYWORDS = {
    "election": 10, "president": 8, "congress": 10, "senate": 10,
    "parliament": 8, "government": 5, "nato": 10, "russia": 6,
    "ukraine": 6, "china": 6, "sanctions": 8, "foreign policy": 10,
    "supreme court": 10, "executive order": 10, "military": 5,
    "diplomacy": 8, "legislation": 8, "immigration": 5,
    "trade deal": 8, "geopolitics": 10,
}
RSSFeedSelection = tuple[Annotated[str, Field(min_length=1, max_length=200)], Literal["bbc-world", "npr-politics"]]
Keyword = Annotated[str, Field(min_length=1, max_length=100)]
KeywordWeight = Annotated[int, Field(strict=True, ge=0, le=100)]


class GlobalChessDatabaseConfigurationError(RuntimeError):
    """Sanitized database configuration failure; never include a connection URL."""


class GlobalChessSettings(BaseSettings):
    app_name: str = Field(default="CivicStudio AI - Trend Scout", min_length=1, max_length=500)
    app_version: str = Field(default="0.1.0", min_length=1, max_length=100,
        validation_alias=AliasChoices("GLOBAL_CHESS_APP_VERSION", "APP_VERSION"))
    research_rss_feeds: tuple[RSSFeedSelection, ...] = Field(default=DEFAULT_RSS_FEEDS, min_length=1, max_length=2)
    ingestion_rss_feeds: tuple[RSSFeedSelection, ...] = Field(default=DEFAULT_RSS_FEEDS, min_length=1, max_length=2)
    editorial_keywords: dict[Keyword, KeywordWeight] = Field(default_factory=lambda: dict(DEFAULT_KEYWORDS), min_length=1, max_length=100)
    relevance_weight: float = Field(default=0.7, ge=0, le=1, allow_inf_nan=False)
    freshness_weight: float = Field(default=0.3, ge=0, le=1, allow_inf_nan=False)
    freshness_window_hours: float = Field(default=72.0, gt=0, allow_inf_nan=False)
    missing_date_freshness: float = Field(default=40.0, ge=0, le=100, allow_inf_nan=False)
    # Temporary direct-classifier configuration, independent of Core settings.
    ollama_base_url: SecretStr = Field(default=SecretStr("http://localhost:12400"), repr=False,
        validation_alias=AliasChoices("GLOBAL_CHESS_OLLAMA_BASE_URL", "OLLAMA_BASE_URL"))
    ollama_model: str = Field(default="qwen2.5:3b",
        validation_alias=AliasChoices("GLOBAL_CHESS_OLLAMA_MODEL", "OLLAMA_MODEL"))
    core_base_url: str | None = None
    core_api_key: SecretStr | None = Field(default=None, repr=False)
    core_request_timeout: float = Field(default=190.0, gt=0, le=600, allow_inf_nan=False)
    database_url: SecretStr | None = Field(default=None, repr=False)
    environment: str = "development"
    allow_legacy_database_url: bool = True
    # Temporary compatibility inputs, read independently of app.core.config.
    legacy_database_url: SecretStr | None = Field(default=None, validation_alias="DATABASE_URL", repr=False, exclude=True)
    legacy_environment: str | None = Field(default=None, validation_alias="ENVIRONMENT", repr=False, exclude=True)

    @field_validator("research_rss_feeds", "ingestion_rss_feeds")
    @classmethod
    def unique_approved_feeds(cls, value):
        if len({feed_id for _, feed_id in value}) != len(value):
            raise ValueError("Feed identifiers must be unique")
        return value

    @model_validator(mode="after")
    def scoring_weights_sum_to_one(self):
        if not math.isclose(self.relevance_weight + self.freshness_weight, 1.0, abs_tol=1e-9):
            raise ValueError("Editorial scoring weights must sum to one")
        return self

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


global_chess_settings = GlobalChessSettings()

# Compatibility exports retain their existing shapes and separate feed ordering.
RESEARCH_RSS_FEEDS = global_chess_settings.research_rss_feeds
INGESTION_RSS_FEEDS = global_chess_settings.ingestion_rss_feeds
FEEDS = {label: APPROVED_FEED_URLS[feed_id] for label, feed_id in INGESTION_RSS_FEEDS}
KEYWORDS = global_chess_settings.editorial_keywords
