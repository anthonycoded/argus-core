from fastapi import Depends, HTTPException, Response, status

from app.core.auth import (
    AuthenticatedApplication,
    get_current_application,
)
from app.services.rate_limiter import (
    RateLimit,
    enforce_rate_limit,
)


def require_rate_limit(
    *,
    bucket: str,
    rate_limit: RateLimit,
):
    async def check_rate_limit(
        response: Response,
        auth: AuthenticatedApplication = Depends(
            get_current_application
        ),
    ) -> AuthenticatedApplication:

        decision = await enforce_rate_limit(
            application_id=auth.application.id,
            api_key_id=auth.api_key.id,
            bucket=bucket,
            rate_limit=rate_limit,
        )

        headers = {
            "X-RateLimit-Limit": str(decision.limit),
            "X-RateLimit-Remaining": str(decision.remaining),
        }

        if not decision.allowed:
            headers["Retry-After"] = str(decision.retry_after)

            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Please try again later.",
                headers=headers,
            )

        response.headers.update(headers)

        return auth

    return check_rate_limit
