from collections.abc import Sequence
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI

from app.core.config import settings
from app.core.database import Base, engine
from app import models  # noqa: F401 - registers SQLAlchemy models
from app.routes import analysis, health, ingestion, research, topics


DEFAULT_ROUTERS: tuple[APIRouter, ...] = (
    health.router,
    ingestion.router,
    topics.router,
    analysis.router,
    research.router,
)


def create_app(
    *,
    title: str | None = None,
    version: str | None = None,
    routers: Sequence[APIRouter] | None = None,
    initialize_database: bool = True,
) -> FastAPI:

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if initialize_database:
            Base.metadata.create_all(bind=engine)

        yield

    application = FastAPI(
        title=title or settings.app_name,
        version=version or settings.app_version,
        lifespan=lifespan,
    )

    for router in routers if routers is not None else DEFAULT_ROUTERS:
        application.include_router(router)

    return application