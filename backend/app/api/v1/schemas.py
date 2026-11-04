from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class SourceRecord(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        str_strip_whitespace=True,
    )

    title: str = Field(min_length=1, max_length=1000)
    url: HttpUrl
    canonical_url: HttpUrl | None = None
    domain: str | None = None
    content_hash: str | None = None
    publisher: str | None = None
    published_at: datetime | None = None
    source_type: str = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)


class SourceRegistrationRequest(BaseModel):
    title: str = Field(min_length=1, max_length=1000)
    url: HttpUrl
    content: str | None = None
    publisher: str | None = None
    published_at: datetime | None = None
    source_type: str = "unknown"
    metadata: dict[str, Any] = Field(default_factory=dict)

    
class SourceBatchRegistrationRequest(BaseModel):
    sources: list[SourceRegistrationRequest] = Field(
        min_length=1,
        max_length=100,
    )


class SourceRegistryResponse(BaseModel):
    id: str
    title: str
    url: str
    canonical_url: str
    domain: str
    content_hash: str | None = None
    publisher: str | None = None
    published_at: datetime | None = None
    source_type: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    
class SourceBatchResult(BaseModel):
    status: Literal["created", "duplicate", "failed"]
    title: str | None = None
    source_id: str | None = None
    canonical_url: str | None = None
    content_hash: str | None = None
    reason: str | None = None


class SourceBatchRegistrationResponse(BaseModel):
    status: Literal["completed", "completed_with_errors"]
    total: int
    created: int
    duplicates: int
    failed: int
    results: list[SourceBatchResult] = Field(default_factory=list)
    