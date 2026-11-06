from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RSSDiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["rss"]
    feed_id: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    limit: int = Field(default=75, ge=1, le=75, strict=True)
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)


class RSSProvenance(BaseModel):
    entry_index: int
    original_url: str = Field(max_length=2048)
    provider_item_id: str | None = Field(default=None, max_length=2048)
    published_raw: str | None = Field(default=None, max_length=512)
    date_field: str | None = None
    reported_publisher: str | None = Field(default=None, max_length=512)
    warnings: list[str] = Field(default_factory=list)


class RSSCandidate(BaseModel):
    provider: Literal["rss"] = "rss"
    source_type: Literal["rss"] = "rss"
    title: str = Field(min_length=1, max_length=8192)
    url: str = Field(max_length=2048)
    canonical_url: str = Field(max_length=2048)
    domain: str = Field(max_length=253)
    publisher: str = Field(max_length=255)
    published_at: datetime | None = None
    snippet: str | None = Field(default=None, max_length=65536)
    provenance: RSSProvenance


class RSSRejectedEntry(BaseModel):
    entry_index: int
    reason: Literal["missing_title", "invalid_url", "field_limit"]


RSSStatus = Literal[
    "success", "incomplete", "unknown_feed", "invalid_cursor", "cursor_expired",
    "timeout", "malformed_feed", "upstream_error", "throttled", "unsafe_destination",
    "response_too_large", "parser_limit", "unsupported_encoding", "storage_unavailable", "busy",
]


class RSSDiscoveryResult(BaseModel):
    provider: Literal["rss"] = "rss"
    feed_id: str
    feed_label: str | None = None
    feed_url: str | None = None
    feed_revision: str | None = None
    feed_title: str | None = None
    format: Literal["rss2", "atom"] | None = None
    status: RSSStatus
    candidates: list[RSSCandidate] = Field(default_factory=list, max_length=75)
    rejected_entries: list[RSSRejectedEntry] = Field(default_factory=list, max_length=75)
    snapshot_id: str | None = None
    fetched_at: datetime | None = None
    expires_at: datetime | None = None
    total_entries: int = 0
    invalid_entries: int = 0
    duplicate_observations: int = 0
    offset: int = 0
    entries_in_page: int = 0
    has_more: bool = False
    next_cursor: str | None = None
    error: str | None = None
    retry_after: str | None = None


RSS_ERRORS = {
    "unknown_feed": (422, "Unknown or disabled RSS feed."),
    "invalid_cursor": (400, "Invalid RSS cursor."),
    "cursor_expired": (410, "RSS snapshot expired; start a new discovery request."),
    "storage_unavailable": (503, "RSS snapshot storage is unavailable."),
    "busy": (503, "RSS feed refresh is in progress."),
}


class RSSFault(Exception):
    def __init__(self, status: RSSStatus, retry_after: str | None = None):
        from app.services.discovery.contracts import ERRORS
        self.status = status
        self.retry_after = retry_after
        super().__init__(({**ERRORS, **RSS_ERRORS})[status][1])

    def result(self, feed_id: str) -> RSSDiscoveryResult:
        return RSSDiscoveryResult(feed_id=feed_id, status=self.status, error=str(self), retry_after=self.retry_after)
