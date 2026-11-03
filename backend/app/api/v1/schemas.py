from datetime import datetime
from typing import Any

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