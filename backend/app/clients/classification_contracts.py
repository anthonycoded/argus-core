"""Global Chess public classification coverage; no provider execution metadata."""

from datetime import datetime, timezone
import json
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.clients.discovery_contracts import validated_article_url
from app.clients.argus_core_client import validated_retry_after

Reason = Literal[
    "prompt_too_large", "invalid_response", "invalid_output", "authentication_failed",
    "permission_denied", "rate_limited", "service_unavailable", "provider_error",
    "timeout", "request_failed", "candidate_limit", "workload_deadline", "cancelled", "discovery_incomplete",
]
MAX_OBSERVATIONS = 225  # 150 evaluations plus at most one fetched 75-item batch.
MAX_COVERAGE_BYTES = 96 * 1024 * 1024


def _json_utf8_size(value: dict) -> int:
    """Count the API's compact UTF-8 JSON without allocating the whole body.

    Coverage's validated field lengths/cardinality bound the projection and each
    escaped string token. Only one token is encoded at a time. Keep these options
    aligned with Starlette JSONResponse; no response serialization is changed.
    """
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return sum(len(token.encode("utf-8")) for token in encoder.iterencode(value))


class PublicCoverageModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class CoverageProvenance(PublicCoverageModel):
    provider: Literal["rss", "google_news"] | None = None
    canonical_url: str | None = Field(default=None, max_length=2048)
    original_url: str | None = Field(default=None, max_length=2048)
    feed_id: Literal["bbc-world", "npr-politics"] | None = None
    snapshot_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    entry_index: int | None = Field(default=None, ge=0, lt=500)

    @field_validator("canonical_url", "original_url")
    @classmethod
    def safe_url(cls, value, info):
        if value is None:
            return None
        if info.field_name == "canonical_url" or value.startswith(("http://", "https://")):
            return validated_article_url(value)
        parts = urlsplit(value)
        if parts.scheme or parts.username or parts.password:
            raise ValueError("Invalid relative provenance URL")
        return value


class CoverageSource(PublicCoverageModel):
    title: str = Field(min_length=1, max_length=1000)
    url: str = Field(max_length=2048)
    publisher: str | None = Field(default=None, max_length=255)
    published_at: datetime | None = None
    snippet: str | None = Field(default=None, max_length=65536)
    source_type: Literal["rss", "google_news", "news"] = "news"
    provenance: CoverageProvenance = Field(default_factory=CoverageProvenance)

    _url = field_validator("url")(validated_article_url)

    @field_validator("published_at", mode="before")
    @classmethod
    def utc_date(cls, value):
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value is not None:
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Coverage publication dates must have a timezone")
            return value.astimezone(timezone.utc)
        return None

    @classmethod
    def from_candidate(cls, item: dict):
        metadata = item.get("provider_metadata") or {}
        provenance = metadata.get("provenance") or {}
        return cls(**{name: item[name] for name in (
            "title", "url", "publisher", "published_at", "snippet", "source_type",
        ) if name in item}, provenance=CoverageProvenance(
            **{name: metadata[name] for name in ("provider", "canonical_url", "feed_id", "snapshot_id") if name in metadata},
            **{name: provenance[name] for name in ("original_url", "entry_index") if name in provenance},
        ))


class UnclassifiedCandidate(PublicCoverageModel):
    status: Literal["failed", "not_attempted"]
    reason: Reason
    source: CoverageSource


class ClassificationCoverage(PublicCoverageModel):
    complete: bool = True
    attempted: int = Field(default=0, ge=0, le=150)
    classified: int = Field(default=0, ge=0, le=150)
    failed: int = Field(default=0, ge=0, le=150)
    not_attempted: int = Field(default=0, ge=0, le=MAX_OBSERVATIONS)
    stop_reason: Reason | None = None
    retry_after: str | None = Field(default=None, max_length=128)
    unclassified: list[UnclassifiedCandidate] = Field(default_factory=list, max_length=MAX_OBSERVATIONS)

    @field_validator("retry_after")
    @classmethod
    def retry(cls, value):
        if value is not None and validated_retry_after(value) != value:
            raise ValueError("Invalid retry guidance")
        return value

    @model_validator(mode="after")
    def coherent(self):
        if (self.attempted != self.classified + self.failed
                or self.failed != sum(item.status == "failed" for item in self.unclassified)
                or self.not_attempted != sum(item.status == "not_attempted" for item in self.unclassified)
                or self.complete != (not self.unclassified and self.stop_reason is None)):
            raise ValueError("Inconsistent classification coverage")
        # A structural safety assertion, not a practical response-size budget.
        # Original descriptions are never shortened to meet it.
        if _json_utf8_size(self.model_dump(mode="json")) > MAX_COVERAGE_BYTES:
            raise ValueError("Classification coverage exceeds its response bound")
        return self
