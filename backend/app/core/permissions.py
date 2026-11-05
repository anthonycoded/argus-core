from fastapi import Depends, HTTPException, status

from app.core.auth import (
    AuthenticatedApplication,
    get_current_application,
)


def require_scopes(*required_scopes: str):
    """Create a dependency that enforces API-key scopes."""

    def check_scopes(
        auth: AuthenticatedApplication = Depends(
            get_current_application
        ),
    ) -> AuthenticatedApplication:

        missing_scopes = [
            scope
            for scope in required_scopes
            if scope not in auth.scopes
        ]

        if missing_scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient API permissions.",
            )

        return auth

    return check_scopes