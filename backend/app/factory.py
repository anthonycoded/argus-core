from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from app.core.config import settings


def create_app(
    *,
    title: str | None = None,
    version: str | None = None,
    routers: Sequence[APIRouter] = (),
    initialize_database: Callable[[], None] | None = None,
    on_shutdown: Callable[[], Awaitable[None]] | None = None,
    audit_session_factory: Callable | None = None,
) -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            if initialize_database is not None:
                initialize_database()
            yield
        finally:
            if on_shutdown is not None:
                await on_shutdown()

    application = FastAPI(
        title=title or settings.app_name,
        version=version or settings.app_version,
        lifespan=lifespan,
    )

    for router in routers:
        application.include_router(router)

    if audit_session_factory is not None:
        from app.core.audit import RequestAuditMiddleware

        application.add_middleware(
            RequestAuditMiddleware,
            session_factory=audit_session_factory,
        )

    return application
