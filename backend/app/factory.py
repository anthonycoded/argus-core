from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from app.core.config import settings


def create_app(
    *,
    title: str | None = None,
    version: str | None = None,
    routers: Sequence[APIRouter] = (),
    initialize_database: Callable[[], None] | None = None,
) -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if initialize_database is not None:
            initialize_database()
        yield

    application = FastAPI(
        title=title or settings.app_name,
        version=version or settings.app_version,
        lifespan=lifespan,
    )

    for router in routers:
        application.include_router(router)

    return application