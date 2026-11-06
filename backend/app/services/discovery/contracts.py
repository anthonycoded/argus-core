from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field


DiscoveryStatus = Literal[
    "success", "timeout", "malformed_feed", "upstream_error", "throttled",
    "unsafe_destination", "response_too_large", "parser_limit", "unsupported_encoding",
]


class DiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: Literal["google_news"] = "google_news"
    query: str = Field(min_length=1, max_length=1000, pattern=r"^[^\x00-\x1f\x7f]+$")
    limit: int = Field(default=10, ge=1, le=75, strict=True)


class CandidateProvenance(BaseModel):
    provider_item_id: str | None = Field(default=None, max_length=2048)
    original_url: str = Field(max_length=2048)
    published_raw: str | None = Field(default=None, max_length=256)
    publisher_url: str | None = Field(default=None, max_length=2048)
    locale: Literal["US:en"] = "US:en"
    truncated_fields: list[Literal["title", "publisher", "snippet"]] = Field(default_factory=list, max_length=3)


class DiscoveryCandidate(BaseModel):
    title: str = Field(min_length=1, max_length=1000)
    url: str = Field(max_length=2048)
    canonical_url: str = Field(max_length=2048)
    domain: str = Field(max_length=253)
    publisher: str = Field(max_length=255)
    published_at: datetime | None = None
    snippet: str | None = Field(default=None, max_length=16384)
    provider: Literal["google_news"] = "google_news"
    source_type: Literal["google_news"] = "google_news"
    provenance: CandidateProvenance


class DiscoveryProviderResult(BaseModel):
    provider: Literal["google_news"] = "google_news"
    status: DiscoveryStatus = "success"
    candidates: list[DiscoveryCandidate] = Field(default_factory=list, max_length=75)
    invalid_candidates: int = 0
    duplicates: int = 0
    truncated: bool = False
    error: str | None = None
    retry_after: str | None = None


ERRORS: dict[str, tuple[int, str]] = {
    "timeout": (504, "Discovery provider timed out."),
    "malformed_feed": (502, "Discovery provider returned an invalid RSS feed."),
    "upstream_error": (502, "Discovery provider is unavailable."),
    "throttled": (429, "Discovery provider is rate limited."),
    "unsafe_destination": (502, "Discovery destination was rejected."),
    "response_too_large": (502, "Discovery response exceeds size limits."),
    "parser_limit": (502, "Discovery feed exceeds parsing limits."),
    "unsupported_encoding": (502, "Discovery response encoding is unsupported."),
}


class DiscoveryError(Exception):
    def __init__(self, status: DiscoveryStatus, retry_after: str | None = None):
        self.status = status
        self.retry_after = retry_after
        super().__init__(ERRORS[status][1])

    def result(self) -> DiscoveryProviderResult:
        return DiscoveryProviderResult(status=self.status, error=str(self), retry_after=self.retry_after)


class DiscoveryFetcher(Protocol):
    async def fetch_google_news(self, query: str) -> bytes: ...


class DiscoveryProvider(Protocol):
    async def discover(self, request: DiscoveryRequest) -> DiscoveryProviderResult: ...
