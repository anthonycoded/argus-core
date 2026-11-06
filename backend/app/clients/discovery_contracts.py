"""Global Chess's wire-contract adapter; no Core implementation dependencies."""

import re
import ipaddress
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, field_validator

_http_url = TypeAdapter(HttpUrl)


def validated_article_url(value: str) -> str:
    # Validate without fetching, rewriting or truncating Core's candidate URL.
    if len(value) > 2048 or "\\" in value or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Invalid discovery URL")
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ValueError("Invalid discovery URL")
    parts = urlsplit(value)
    if not parts.netloc or parts.scheme not in {"http", "https"}:
        raise ValueError("Invalid discovery URL")
    parsed = _http_url.validate_python(value)
    if parsed.username or parsed.password:
        raise ValueError("Invalid discovery URL")
    host = (parsed.host or "").strip("[]").rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Invalid discovery URL")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")):
            raise ValueError("Invalid discovery URL")
    else:
        if (not address.is_global or address.is_multicast or address.is_reserved
                or (address.version == 6 and address.ipv4_mapped is not None)):
            raise ValueError("Invalid discovery URL")
    return value


class GoogleNewsDiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, hide_input_in_errors=True)
    provider: Literal["google_news"] = "google_news"
    query: str = Field(min_length=1, max_length=1000, pattern=r"^[^\x00-\x1f\x7f]+$")
    limit: int = Field(default=10, ge=1, le=75, strict=True)


class ProviderProvenance(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    original_url: str = Field(max_length=2048)
    provider_item_id: str | None = Field(default=None, max_length=2048)
    published_raw: str | None = Field(default=None, max_length=256)
    publisher_url: str | None = Field(default=None, max_length=2048)
    locale: Literal["US:en"]
    truncated_fields: list[Literal["title", "publisher", "snippet"]] = Field(max_length=3)

    _original_url = field_validator("original_url")(validated_article_url)

    @field_validator("publisher_url")
    @classmethod
    def validate_publisher_url(cls, value):
        return validated_article_url(value) if value is not None else None


class CoreDiscoveryCandidate(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    title: str = Field(min_length=1, max_length=1000)
    url: str = Field(max_length=2048)
    canonical_url: str = Field(max_length=2048)
    domain: str = Field(min_length=1, max_length=253)
    publisher: str = Field(max_length=255)
    published_at: datetime | None
    snippet: str | None = Field(max_length=16384)
    provider: Literal["google_news"]
    source_type: Literal["google_news"]
    provenance: ProviderProvenance

    _urls = field_validator("url", "canonical_url")(validated_article_url)

    @field_validator("published_at", mode="before")
    @classmethod
    def require_date_string(cls, value):
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("Publication date must be an ISO timestamp")
        return value

    @field_validator("published_at")
    @classmethod
    def utc_date(cls, value):
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Publication date must have a timezone")
            return value.astimezone(timezone.utc)
        return None

    def research_source(self) -> dict:
        """Editorial code gets its existing fields, plus transient provenance."""
        return {
            "title": self.title, "url": self.url, "publisher": self.publisher,
            "published_at": self.published_at, "snippet": self.snippet,
            "source_type": self.source_type,
            "provider_metadata": {
                "provider": self.provider, "canonical_url": self.canonical_url,
                "domain": self.domain, "provenance": self.provenance.model_dump(mode="json"),
            },
        }


class CoreDiscoveryResult(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    provider: Literal["google_news"]
    status: Literal["success"]
    candidates: list[CoreDiscoveryCandidate] = Field(max_length=75)
    invalid_candidates: int = Field(ge=0, strict=True)
    duplicates: int = Field(ge=0, strict=True)
    truncated: bool = Field(strict=True)
    error: None
    retry_after: None

    def research_sources(self) -> list[dict]:
        return [candidate.research_source() for candidate in self.candidates]
