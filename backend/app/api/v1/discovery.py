from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse

from app.core.permissions import require_scopes
from app.core.auth import AuthenticatedApplication, get_current_application
from app.core.rate_limit import require_rate_limit
from app.services.discovery.contracts import (
    ERRORS, DiscoveryError, DiscoveryProvider, DiscoveryProviderResult, DiscoveryRequest,
)
from app.services.discovery.google_news import GoogleNewsProvider
from app.services.discovery.rss import RSSProvider
from app.services.discovery.rss_contracts import RSSDiscoveryRequest, RSSDiscoveryResult, RSS_ERRORS, RSSFault
from app.services.discovery.rss_store import RedisSnapshotStore
from app.services.rate_limiter import DISCOVERY_LIMIT, GENERAL_API_LIMIT, RSS_CONTINUATION_LIMIT
from app.core.config import settings
from app.services.discovery.gdelt import GDELTProvider
from app.services.discovery.gdelt_admission import RedisGDELTAdmission
from app.services.discovery.gdelt_contracts import GDELTRequest, GDELTResult, GDELTFault, ERRORS as GDELT_ERRORS

router = APIRouter(prefix="/discovery", tags=["Discovery"])


def get_discovery_provider() -> DiscoveryProvider:
    return GoogleNewsProvider()


def get_rss_provider() -> RSSProvider:
    from app.core.redis import redis_client
    return RSSProvider(RedisSnapshotStore(redis_client))


# Google News retains its existing omitted-provider default. RSS explicitly
# requires provider='rss'; both branches reject unrelated/unknown fields.
DiscoveryAPIRequest = DiscoveryRequest | RSSDiscoveryRequest | GDELTRequest


def get_gdelt_provider() -> GDELTProvider:
    from app.core.redis import redis_client
    return GDELTProvider(RedisGDELTAdmission(redis_client),enabled=settings.gdelt_enabled)


async def require_discovery_rate_limit(
    request: DiscoveryAPIRequest,
    response: Response,
    auth: AuthenticatedApplication = Depends(get_current_application),
):
    # Even invalid cursors use the bounded continuation bucket. RSSProvider's
    # cursor branch must never create/refresh a snapshot or fetch externally.
    continuation = isinstance(request, RSSDiscoveryRequest) and request.cursor is not None
    return await require_rate_limit(
        bucket="rss_continuation" if continuation else "discovery",
        rate_limit=RSS_CONTINUATION_LIMIT if continuation else DISCOVERY_LIMIT,
    )(response=response, auth=auth)


@router.post("", response_model=DiscoveryProviderResult | RSSDiscoveryResult | GDELTResult, responses={
    429: {"description": "Application/key quota or provider throttling"},
    502: {"model": DiscoveryProviderResult}, 504: {"model": DiscoveryProviderResult},
})
async def discover(
    request: DiscoveryAPIRequest,
    _auth=Depends(require_scopes("discovery:read")),
    _general=Depends(require_rate_limit(bucket="general", rate_limit=GENERAL_API_LIMIT)),
    _discovery=Depends(require_discovery_rate_limit),
    provider: DiscoveryProvider = Depends(get_discovery_provider),
    rss_provider: RSSProvider = Depends(get_rss_provider),
    gdelt_provider: GDELTProvider = Depends(get_gdelt_provider),
):
    if isinstance(request,GDELTRequest):
        try:result=await gdelt_provider.discover(request)
        except Exception:result=GDELTFault("upstream_error").result(request)
        if result.status in {"success","incomplete"}:return result
        return JSONResponse(status_code=GDELT_ERRORS[result.status][0],content=result.model_dump(mode="json"),
            headers={"Retry-After":result.retry_after} if result.retry_after is not None else None)
    if isinstance(request, RSSDiscoveryRequest):
        try:
            result = await rss_provider.discover(request, _auth.application.id)
        except Exception:
            result = RSSFault("storage_unavailable").result(request.feed_id)
        if result.status in {"success", "incomplete"}:
            return result
        return JSONResponse(status_code=({**ERRORS, **RSS_ERRORS})[result.status][0],
            content=result.model_dump(mode="json"),
            headers={"Retry-After": result.retry_after} if result.retry_after is not None else None)
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
