from datetime import datetime, timezone
from urllib.parse import urlsplit
from sqlalchemy.orm import Session
from sqlalchemy import Select
from fastapi import APIRouter, Depends, HTTPException

from app.core.database import get_db
from app.core.models import SourceRecordDB

from app.api.v1.schemas import SourceRecord, SourceRegistrationRequest, SourceRegistryResponse

from app.utils.source_normalization import normalize_url
from app.utils.deduplication import content_hash

from app.services.source_registry import register_source


router = APIRouter(
    prefix="/sources",
    tags=["Sources"],
)


@router.post("/normalize", response_model=SourceRecord)
def normalize_source(
    request: SourceRecord,
    content: str | None = None,
):
    normalized_url = normalize_url(str(request.url))

    published_at = request.published_at

    if published_at is not None:
        if published_at.tzinfo is None:
            published_at = published_at.replace(tzinfo=timezone.utc)
        else:
            published_at = published_at.astimezone(timezone.utc)

    fingerprint = (
        content_hash(content)
        if content is not None
        else request.content_hash
    )

    return SourceRecord(
        title=request.title,
        url=normalized_url,
        canonical_url=normalized_url,
        domain=urlsplit(normalized_url).netloc,
        content_hash=fingerprint,
        publisher=request.publisher,
        published_at=published_at,
        source_type=request.source_type,
        metadata=request.metadata,
    )

@router.post("/register")
def register_source_endpoint(
    request: SourceRegistrationRequest,
    db: Session = Depends(get_db),
):
    try:
        return register_source(
            db,
            title=request.title,
            url=str(request.url),
            content=request.content,
            publisher=request.publisher,
            published_at=request.published_at,
            source_type=request.source_type,
            metadata=request.metadata,
        )

    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Source registration failed.",
        ) from exc

@router.get("/{source_id}", response_model=SourceRegistryResponse)
def get_registered_source(
    source_id: str,
    db: Session = Depends(get_db),
):
    source = db.get(SourceRecordDB, source_id)

    if source is None:
        raise HTTPException(
            status_code=404,
            detail="Source not found.",
        )

    return {
        "id": source.id,
        "title": source.title,
        "url": source.url,
        "canonical_url": source.canonical_url,
        "domain": source.domain,
        "content_hash": source.content_hash,
        "publisher": source.publisher,
        "published_at": source.published_at,
        "source_type": source.source_type,
        "metadata": source.metadata_json,
        "created_at": source.created_at,
    }