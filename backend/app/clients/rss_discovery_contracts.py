"""Global Chess-owned RSS wire models; independent of Core implementation."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.clients.discovery_contracts import CoreDiscoveryCandidate


class RSSRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    provider: Literal["rss"] = "rss"
    feed_id: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    limit: int = Field(default=75, strict=True, ge=1, le=75)
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)


class RSSProvenance(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    entry_index: int = Field(strict=True, ge=0, lt=500)
    # RSS may supply a relative original URL; Core's resolved URL is validated.
    original_url: str = Field(max_length=2048)
    provider_item_id: str | None = Field(default=None, max_length=2048)
    published_raw: str | None = Field(default=None, max_length=512)
    date_field: str | None = Field(default=None, max_length=64)
    reported_publisher: str | None = Field(default=None, max_length=512)
    warnings: list[str] = Field(max_length=16)


class RSSCandidate(CoreDiscoveryCandidate):
    provider: Literal["rss"]
    source_type: Literal["rss"]
    title: str = Field(min_length=1, max_length=8192)
    snippet: str | None = Field(max_length=65536)
    provenance: RSSProvenance


class RSSRejectedEntry(BaseModel):
    entry_index: int = Field(strict=True, ge=0, lt=500)
    reason: Literal["missing_title", "invalid_url", "field_limit"]


class RSSPage(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)
    provider: Literal["rss"]
    feed_id: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    feed_label: str = Field(min_length=1, max_length=255)
    feed_url: str = Field(max_length=2048)
    feed_revision: str = Field(min_length=1, max_length=128)
    feed_title: str | None = Field(max_length=8192)
    format: Literal["rss2", "atom"]
    status: Literal["success", "incomplete"]
    candidates: list[RSSCandidate] = Field(max_length=75)
    rejected_entries: list[RSSRejectedEntry] = Field(max_length=75)
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    fetched_at: datetime
    expires_at: datetime
    total_entries: int = Field(strict=True, ge=0, le=500)
    invalid_entries: int = Field(strict=True, ge=0, le=500)
    duplicate_observations: int = Field(strict=True, ge=0, le=500)
    offset: int = Field(strict=True, ge=0, le=500)
    entries_in_page: int = Field(strict=True, ge=0, le=75)
    has_more: bool = Field(strict=True)
    next_cursor: str | None = Field(min_length=1, max_length=1024)
    error: None
    retry_after: None

    _date_type = field_validator("fetched_at", "expires_at", mode="before")(CoreDiscoveryCandidate.require_date_string.__func__)
    _dates = field_validator("fetched_at", "expires_at")(CoreDiscoveryCandidate.utc_date.__func__)

    @model_validator(mode="after")
    def coherent_page(self):
        indices = [c.provenance.entry_index for c in self.candidates]
        rejected = [r.entry_index for r in self.rejected_entries]
        end = self.offset + self.entries_in_page
        if (sorted(indices + rejected) != list(range(self.offset, end))
                or indices != sorted(indices) or rejected != sorted(rejected)
                or end > self.total_entries
                or self.has_more != (end < self.total_entries)
                or self.has_more != (self.next_cursor is not None)
                or (self.has_more and not self.entries_in_page)
                or self.invalid_entries > self.total_entries
                or self.duplicate_observations > self.total_entries - self.invalid_entries
                or len(rejected) > self.invalid_entries
                or self.expires_at <= self.fetched_at
                or (self.status == "success" and (self.invalid_entries or any(c.provenance.warnings for c in self.candidates)))):
            raise ValueError("Inconsistent RSS page")
        return self

    def identity(self) -> dict:
        return self.model_dump(exclude={"candidates", "rejected_entries", "offset", "entries_in_page", "has_more", "next_cursor"})

    def research_sources(self) -> list[dict]:
        result = []
        for candidate in self.candidates:
            source = candidate.research_source()
            source["provider_metadata"].update(
                publisher=candidate.publisher,
                feed_id=self.feed_id, feed_label=self.feed_label, feed_url=self.feed_url,
                feed_revision=self.feed_revision, feed_title=self.feed_title, format=self.format,
                snapshot_id=self.snapshot_id, fetched_at=self.fetched_at.isoformat(),
                expires_at=self.expires_at.isoformat(), snapshot_status=self.status,
                total_entries=self.total_entries, invalid_entries=self.invalid_entries,
                duplicate_observations=self.duplicate_observations,
            )
            result.append(source)
        return result
