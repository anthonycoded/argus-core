from datetime import datetime, timezone
from urllib.parse import urlsplit
from sqlalchemy.orm import Session
from sqlalchemy import select, func
from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.v1.schemas import (
    SourceRecord, 
    SourceRegistrationRequest, 
    SourceRegistryResponse,   
    SourceBatchRegistrationRequest, 
    SourceBatchRegistrationResponse)

from app.core.database import get_db
from app.core.models import SourceRecordDB
from app.core.permissions import require_scopes

from app.utils.source_normalization import normalize_url
from app.utils.deduplication import content_hash

from app.services.source_registry import register_source, register_source_batch


router = APIRouter(
    prefix="/sources",
    tags=["Sources"],
)


@router.post("/normalize", response_model=SourceRecord)
def normalize_source(
    request: SourceRecord,
    content: str | None = None,
    _auth=Depends(require_scopes("sources:write")),
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
    _auth=Depends(require_scopes("sources:write")),
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


@router.post(
    "/register/batch",
    response_model=SourceBatchRegistrationResponse,
)
def register_source_batch_endpoint(
    request: SourceBatchRegistrationRequest,
    db: Session = Depends(get_db),
    _auth=Depends(require_scopes("sources:write")),
):
    sources = []

    for source in request.sources:
        source_data = source.model_dump()
        source_data["url"] = str(source.url)
        sources.append(source_data)

    return register_source_batch(db, sources)


@router.get("/{source_id}", response_model=SourceRegistryResponse)
def get_registered_source(
    source_id: str,
    db: Session = Depends(get_db),
    _auth=Depends(require_scopes("sources:read")),
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

@router.get("")
def list_registered_sources(
    db: Session = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    publisher: str | None = None,
    source_type: str | None = None,
    domain: str | None = None,
    search: str | None = Query(default=None, min_length=1, max_length=200),
    _auth=Depends(require_scopes("sources:read")),
):
    statement = select(SourceRecordDB)
    count_statement = select(func.count()).select_from(SourceRecordDB)

    if publisher:
        condition = SourceRecordDB.publisher.ilike(f"%{publisher}%")
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)

    if source_type:
        condition = SourceRecordDB.source_type == source_type
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)

    if domain:
        condition = SourceRecordDB.domain == domain.strip().lower()
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)

    if search:
        condition = SourceRecordDB.title.ilike(f"%{search}%")
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)

    total = db.scalar(count_statement) or 0

    statement = (
        statement
        .order_by(SourceRecordDB.created_at.desc())
        .offset(offset)
        .limit(limit)
    )

    sources = db.scalars(statement).all()

    return {
        "items": [
            {
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
            for source in sources
        ],
        "limit": limit,
        "offset": offset,
        "count": len(sources),
        "total": total,
    }
