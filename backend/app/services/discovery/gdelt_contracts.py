"""Bounded, provider-specific DOC discovery contracts; no editorial policy."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def compile_query(value: str) -> str:
    if not value or len(value) > 1000 or len(value.encode("utf-8")) > 4096:
        raise ValueError("Invalid GDELT query size")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Query controls are forbidden")
    # No native operators/qualifiers, escaping, parentheses or wildcard grammar.
    units = []
    words = 0
    position = 0
    for match in re.finditer(r'"([^"\n]+)"|([^\s"]+)', value):
        if value[position:match.start()].strip() or (position and match.start() == position):
            raise ValueError("Query units must be separated")
        phrase, word = match.groups()
        parts = (phrase or word).split()
        if not parts or any(not p.isalnum() or len(p) > 80 for p in parts):
            raise ValueError("Only words and quoted phrases are supported")
        if word and word.upper() in {"AND", "OR", "NOT", "NEAR"}:
            raise ValueError("Query operators are unsupported")
        words += len(parts)
        units.append('"' + " ".join(parts) + '"' if phrase else word)
        position = match.end()
    if value[position:].strip() or not units or len(units) > 16 or words > 32:
        raise ValueError("Unsupported query complexity")
    return " ".join(units)


class GDELTRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    provider: Literal["gdelt"]
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=25, strict=True, ge=1, le=75)
    window_hours: int = Field(default=24, strict=True, ge=1, le=168)

    @field_validator("query")
    @classmethod
    def query_grammar(cls, value):
        return compile_query(value)


class GDELTProvenance(BaseModel):
    entry_index: int = Field(ge=0, lt=75)
    original_url: str = Field(max_length=2048)
    reported_domain: str | None = Field(default=None, max_length=255)
    provider_item_id: str | None = Field(default=None, max_length=2048)
    seen_raw: str | None = Field(default=None, max_length=128)
    seen_at: datetime | None = None
    language: str | None = Field(default=None, max_length=64)
    source_country: str | None = Field(default=None, max_length=128)
    original_mobile_url: str | None = Field(default=None, max_length=2048)
    mobile_url: str | None = Field(default=None, max_length=2048)
    original_image_url: str | None = Field(default=None, max_length=2048)
    image_url: str | None = Field(default=None, max_length=2048)
    warnings: list[Literal["invalid_seen_date", "invalid_mobile_url", "invalid_image_url"]] = Field(default_factory=list, max_length=3)


class GDELTCandidate(BaseModel):
    title: str = Field(min_length=1, max_length=1000)
    url: str = Field(max_length=2048)
    canonical_url: str = Field(max_length=2048)
    domain: str = Field(max_length=253)
    publisher: str = Field(max_length=255)
    provider: Literal["gdelt"] = "gdelt"
    source_type: Literal["gdelt"] = "gdelt"
    published_at: None = None
    snippet: None = None
    provenance: GDELTProvenance


class GDELTRejectedEntry(BaseModel):
    entry_index: int = Field(ge=0, lt=75)
    reason: Literal["invalid_record", "invalid_url", "field_limit"]


GDELTStatus = Literal[
    "success", "incomplete", "disabled", "throttled", "busy", "paused",
    "storage_unavailable", "timeout", "malformed_response", "upstream_error",
    "unsafe_destination", "response_too_large", "parser_limit", "unsupported_encoding",
]


class GDELTResult(BaseModel):
    provider: Literal["gdelt"] = "gdelt"
    status: GDELTStatus
    limit: int = Field(ge=1, le=75)
    window_hours: int = Field(ge=1, le=168)
    candidates: list[GDELTCandidate] = Field(default_factory=list, max_length=75)
    rejected_entries: list[GDELTRejectedEntry] = Field(default_factory=list, max_length=75)
    received_count: int = Field(default=0, ge=0, le=75)
    invalid_candidates: int = Field(default=0, ge=0, le=75)
    duplicate_observations: int = Field(default=0, ge=0, le=75)
    limit_reached: bool = False
    fetched_at: datetime | None = None
    error: str | None = None
    retry_after: str | None = None


ERRORS = {
    "disabled": (503, "GDELT discovery is disabled."),
    "throttled": (429, "GDELT discovery is rate limited."),
    "busy": (503, "GDELT discovery is busy."),
    "paused": (503, "GDELT discovery requires operator review."),
    "storage_unavailable": (503, "GDELT admission storage is unavailable."),
    "timeout": (504, "GDELT discovery timed out."),
    "malformed_response": (502, "GDELT returned an invalid response."),
    "upstream_error": (502, "GDELT is unavailable."),
    "unsafe_destination": (502, "GDELT destination was rejected."),
    "response_too_large": (502, "GDELT response exceeds size limits."),
    "parser_limit": (502, "GDELT response exceeds parsing limits."),
    "unsupported_encoding": (502, "GDELT content encoding is unsupported."),
}


@dataclass(frozen=True)
class RetryPolicy:
    delay_seconds: int = 0
    until_ms: int = 0
    hold: bool = False


class GDELTFault(Exception):
    def __init__(self, status: GDELTStatus, retry_after: str | None = None, policy: RetryPolicy | None = None):
        self.status = status
        self.retry_after = retry_after
        self.policy = policy
        super().__init__(ERRORS[status][1])

    def result(self, request: GDELTRequest) -> GDELTResult:
        return GDELTResult(status=self.status, limit=request.limit, window_hours=request.window_hours,
                           error=str(self), retry_after=self.retry_after)
