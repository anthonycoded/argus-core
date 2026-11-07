"""Global Chess's FastAPI composition and lifespan. No Core initialization hooks."""

from collections.abc import Awaitable, Callable, Sequence
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from app.global_chess_config import global_chess_settings


def create_app(
    *,
    title: str | None = None,
    version: str | None = None,
    routers: Sequence[APIRouter] = (),
    initialize_database: Callable[[], None] | None = None,
    check_database: Callable[[], Awaitable[None]] | None = None,
    on_shutdown: Callable[[], Awaitable[None]] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            if initialize_database is not None:
                initialize_database()
            if check_database is not None:
                await check_database()
            yield
        finally:
            if on_shutdown is not None:
                await on_shutdown()

    application = FastAPI(
        title=title or global_chess_settings.app_name,
        version=version or global_chess_settings.app_version,
        lifespan=lifespan,
    )
    for router in routers:
        application.include_router(router)
    return application
