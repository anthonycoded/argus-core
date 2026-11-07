"""Global Chess-owned source wire contracts, not Core ORM or evidence storage."""

import hashlib
import re
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, field_validator, model_validator

from app.clients.discovery_contracts import validated_article_url

_http_url = TypeAdapter(HttpUrl)


def source_id(value: str) -> str:
    # Core defaults to UUIDs but its successful contract permits opaque IDs.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,35}", value):
        raise ValueError("Invalid Core source ID")
    return value


def public_source_url(value: str) -> str:
    validated_article_url(value)
    if any(ord(c) == 127 for c in value):
        raise ValueError("Invalid source URL")
    # Reject obvious credential-bearing queries; callers must only supply public facts.
    sensitive = {"key", "api_key", "apikey", "token", "access_token", "password",
                 "secret", "authorization", "signature", "x-amz-signature"}
    if any(key.casefold() in sensitive for key, _ in parse_qsl(urlsplit(value).query)):
        raise ValueError("Credential-bearing source URL")
    return value


def registry_url(value: str) -> str:
    """Mirror the v1 HTTP URL/normalization contract without a Core import."""
    parts = urlsplit(str(_http_url.validate_python(value)))
    query = "&".join(item for item in parts.query.split("&") if item and not item.lower().startswith(
        ("utm_", "fbclid=", "gclid=", "mc_cid=", "mc_eid=")
    ))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/", query, ""))


def article_hash(text: str) -> str:
    """Mirror v1's case/whitespace-insensitive full-article fingerprint."""
    return hashlib.sha256(re.sub(r"\s+", " ", text).strip().lower().encode("utf-8")).hexdigest()


class SourceContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class PublicArticleContent(SourceContract):
    """Explicit full public article input. Never construct from a research snippet."""
    kind: Literal["full_public_article"]
    text: str = Field(min_length=1, max_length=250000, repr=False)

    @field_validator("text")
    @classmethod
    def bounded_article(cls, value):
        if not value.strip() or len(value.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Invalid public article size")
        return value


class SourceRegistration(SourceContract):
    title: str = Field(min_length=1, max_length=1000, repr=False)
    observed_url: str = Field(max_length=2048, repr=False)
    publisher: str | None = Field(default=None, max_length=500, repr=False)
    published_at: datetime | None = None
    source_type: str = Field(default="unknown", min_length=1, max_length=100)
    article: PublicArticleContent | None = Field(default=None, repr=False)

    _url = field_validator("observed_url")(public_source_url)

    @field_validator("title", "publisher", "source_type")
    @classmethod
    def public_text(cls, value):
        if value is not None and (not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError("Invalid source text")
        return value

    @field_validator("published_at")
    @classmethod
    def utc_date(cls, value):
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Publication date needs a timezone")
            return value.astimezone(timezone.utc)
        return None

    @field_validator("published_at", mode="before")
    @classmethod
    def date_type(cls, value):
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("Invalid publication timestamp")
        return value

    def wire_payload(self) -> dict:
        # Explicit allowlist. There is no metadata/snippet/editorial input field.
        payload = self.model_dump(mode="json", include={"title", "publisher", "published_at", "source_type"})
        payload["url"] = self.observed_url
        if self.article is not None:
            payload["content"] = self.article.text
        return payload


class RegistrationReceipt(SourceContract):
    status: Literal["created", "duplicate"]
    source_id: str
    canonical_url: str = Field(max_length=2048, repr=False)
    reason: Literal["canonical_url", "content_hash"] | None = None
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    _id = field_validator("source_id")(source_id)
    _url = field_validator("canonical_url")(public_source_url)

    @model_validator(mode="after")
    def coherent_receipt(self):
        if self.status == "created":
            if self.reason is not None or "content_hash" not in self.model_fields_set:
                raise ValueError("Invalid created receipt")
        elif self.reason is None or self.content_hash is not None:
            raise ValueError("Invalid duplicate receipt")
        if registry_url(self.canonical_url) != self.canonical_url:
            raise ValueError("Noncanonical source URL")
        return self


class RegisteredSource(SourceContract):
    """Shared facts only. Core's arbitrary metadata is intentionally not exported."""
    id: str
    title: str = Field(min_length=1, max_length=1000, repr=False)
    url: str = Field(max_length=2048, repr=False)
    canonical_url: str = Field(max_length=2048, repr=False)
    domain: str = Field(min_length=1, max_length=255)
    content_hash: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    publisher: str | None = Field(max_length=500, repr=False)
    published_at: datetime | None
    source_type: str = Field(min_length=1, max_length=100)
    created_at: datetime

    _id = field_validator("id")(source_id)
    _urls = field_validator("url", "canonical_url")(public_source_url)

    @field_validator("published_at", "created_at", mode="before")
    @classmethod
    def date_type(cls, value):
        if value is not None and not isinstance(value, (str, datetime)):
            raise ValueError("Invalid source timestamp")
        return value

    @model_validator(mode="after")
    def coherent_source(self):
        if (registry_url(self.url) != self.canonical_url
                or registry_url(self.canonical_url) != self.canonical_url
                or urlsplit(self.canonical_url).netloc.lower() != self.domain):
            raise ValueError("Inconsistent source facts")
        return self


class SourceResolution(SourceContract):
    observed_url: str = Field(repr=False)
    observed_publisher: str | None = Field(repr=False)
    receipt: RegistrationReceipt
    source: RegisteredSource
    match: Literal["created", "url_matched", "cross_url_content_matched"]
    content_verification: Literal["not_supplied", "matched", "unavailable"]
    requires_review: bool
