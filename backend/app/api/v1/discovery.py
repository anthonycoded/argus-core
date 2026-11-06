from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.core.permissions import require_scopes
from app.core.rate_limit import require_rate_limit
from app.services.discovery.contracts import (
    ERRORS, DiscoveryError, DiscoveryProvider, DiscoveryProviderResult, DiscoveryRequest,
)
from app.services.discovery.google_news import GoogleNewsProvider
from app.services.rate_limiter import DISCOVERY_LIMIT, GENERAL_API_LIMIT

router = APIRouter(prefix="/discovery", tags=["Discovery"])


def get_discovery_provider() -> DiscoveryProvider:
    return GoogleNewsProvider()


@router.post("", response_model=DiscoveryProviderResult, responses={
    429: {"description": "Application/key quota or provider throttling"},
    502: {"model": DiscoveryProviderResult}, 504: {"model": DiscoveryProviderResult},
})
async def discover(
    request: DiscoveryRequest,
    _auth=Depends(require_scopes("discovery:read")),
    _general=Depends(require_rate_limit(bucket="general", rate_limit=GENERAL_API_LIMIT)),
    _discovery=Depends(require_rate_limit(bucket="discovery", rate_limit=DISCOVERY_LIMIT)),
    provider: DiscoveryProvider = Depends(get_discovery_provider),
):
    try:
        result = await provider.discover(request)
    except Exception:
        # Never return/log upstream URLs, queries, headers, bodies, or exception text.
        result = DiscoveryError("upstream_error").result()
    if result.status == "success":
        return result
    return JSONResponse(
        status_code=ERRORS[result.status][0], content=result.model_dump(mode="json"),
        headers={"Retry-After": result.retry_after} if result.retry_after is not None else None,
    )
