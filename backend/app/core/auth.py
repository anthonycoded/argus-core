from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.models.api_key import APIKeyDB
from app.core.models.application import ApplicationDB
from app.services.api_key_service import verify_api_key


@dataclass(frozen=True)
class AuthenticatedApplication:
    application: ApplicationDB
    api_key: APIKeyDB

    @property
    def scopes(self) -> list[str]:
        return self.api_key.scopes


def get_current_application(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> AuthenticatedApplication:

    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or inactive API key.",
        headers={"WWW-Authenticate": "APIKey"},
    )

    if not x_api_key:
        raise unauthorized

    key_prefix = x_api_key[:16]

    api_key = db.scalar(
        select(APIKeyDB).where(
            APIKeyDB.key_prefix == key_prefix
        )
    )

    if not api_key:
        raise unauthorized

    if not verify_api_key(x_api_key, api_key.key_hash):
        raise unauthorized

    if not api_key.is_active:
        raise unauthorized

    application = db.get(
        ApplicationDB,
        api_key.application_id,
    )

    if not application or not application.is_active:
        raise unauthorized

    now = datetime.now(timezone.utc)

    if api_key.expires_at is not None:
        expires_at = api_key.expires_at

        # Handle databases that return naive timestamps.
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if expires_at <= now:
            raise unauthorized

    api_key.last_used_at = now
    db.commit()

    return AuthenticatedApplication(
        application=application,
        api_key=api_key,
    )