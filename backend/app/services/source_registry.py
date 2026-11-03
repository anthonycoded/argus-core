from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.models import SourceRecordDB
from app.utils.deduplication import canonical_source_url, content_hash
from app.api.v1.schemas import SourceRegistrationRequest


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


def register_source_batch(db: Session, sources: list[dict]) -> dict:
    results = []
    created = 0
    duplicates = 0
    failed = 0

    for source_data in sources:
        try:
            result = register_source(
                db,
                title=source_data["title"],
                url=source_data["url"],
                content=source_data.get("content"),
                publisher=source_data.get("publisher"),
                published_at=source_data.get("published_at"),
                source_type=source_data.get("source_type", "unknown"),
                metadata=source_data.get("metadata"),
            )

            if result["status"] == "created":
                created += 1
            elif result["status"] == "duplicate":
                duplicates += 1

            results.append(result)

        except Exception:
            db.rollback()
            failed += 1
            results.append({
                "status": "failed",
                "title": source_data.get("title"),
                "reason": "Source registration failed.",
            })

    total = len(sources)

    return {
        "status": "completed_with_errors" if failed else "completed",
        "total": total,
        "created": created,
        "duplicates": duplicates,
        "failed": failed,
        "results": results,
    }
