from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.models import SourceRecordDB
from app.utils.deduplication import canonical_source_url, content_hash


def register_source(
    db: Session,
    *,
    title: str,
    url: str,
    content: str | None = None,
    publisher: str | None = None,
    published_at: datetime | None = None,
    source_type: str = "unknown",
    metadata: dict | None = None,
) -> dict:
    """Register a source and detect existing URL or content duplicates."""

    canonical_url = canonical_source_url(url)

    existing = db.scalar(
        select(SourceRecordDB).where(
            SourceRecordDB.canonical_url == canonical_url
        )
    )

    if existing:
        return {
            "status": "duplicate",
            "reason": "canonical_url",
            "source_id": existing.id,
            "canonical_url": existing.canonical_url,
        }

    fingerprint = content_hash(content) if content else None

    if fingerprint:
        existing = db.scalar(
            select(SourceRecordDB).where(
                SourceRecordDB.content_hash == fingerprint
            )
        )

        if existing:
            return {
                "status": "duplicate",
                "reason": "content_hash",
                "source_id": existing.id,
                "canonical_url": existing.canonical_url,
            }

    source = SourceRecordDB(
        title=title,
        url=url,
        canonical_url=canonical_url,
        domain=canonical_url.split("/")[2],
        content_hash=fingerprint,
        publisher=publisher,
        published_at=published_at,
        source_type=source_type,
        metadata_json=metadata or {},
    )

    db.add(source)

    try:
        db.commit()
        db.refresh(source)
    except IntegrityError:
        db.rollback()

        existing = db.scalar(
            select(SourceRecordDB).where(
                SourceRecordDB.canonical_url == canonical_url
            )
        )

        if existing:
            return {
                "status": "duplicate",
                "reason": "canonical_url",
                "source_id": existing.id,
                "canonical_url": existing.canonical_url,
            }

        raise

    return {
        "status": "created",
        "source_id": source.id,
        "canonical_url": source.canonical_url,
        "content_hash": source.content_hash,
    }