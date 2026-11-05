from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.models.api_key import APIKeyDB
from app.core.models.application import ApplicationDB
from app.services.api_key_service import generate_api_key


def issue_api_key(
    db: Session,
    *,
    application_slug: str,
    name: str,
    scopes: list[str],
    environment: str = "live",
) -> dict:
    """Issue an API key for a registered application."""

    application = db.scalar(
        select(ApplicationDB).where(
            ApplicationDB.slug == application_slug
        )
    )

    if not application or not application.is_active:
        raise ValueError("Application does not exist or is inactive.")

    generated = generate_api_key(environment)

    api_key = APIKeyDB(
        application_id=application.id,
        name=name,
        key_prefix=generated.key_prefix,
        key_hash=generated.key_hash,
        scopes=scopes,
    )

    db.add(api_key)
    db.commit()
    db.refresh(api_key)

    return {
        "id": api_key.id,
        "application_slug": application.slug,
        "name": api_key.name,
        "api_key": generated.plaintext_key,
        "scopes": api_key.scopes,
        "created_at": api_key.created_at,
    }


def revoke_api_key(
    db: Session,
    *,
    key_id: str,
) -> bool:
    """Revoke an API key without deleting its audit history."""

    api_key = db.scalar(
        select(APIKeyDB).where(APIKeyDB.id == key_id)
    )

    if not api_key:
        return False

    if not api_key.is_active:
        return True

    api_key.is_active = False
    db.commit()

    return True


def get_api_key_by_prefix(
    db: Session,
    key_prefix: str,
) -> APIKeyDB | None:
    """Retrieve an API key record using its public prefix."""

    return db.scalar(
        select(APIKeyDB).where(
            APIKeyDB.key_prefix == key_prefix
        )
    )