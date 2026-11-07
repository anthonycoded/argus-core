from app.factory import create_app

# Register Global Chess database models.
from app import models  # noqa: F401

from app.database import Base, engine
from app.routes import analysis, health, ingestion, research, topics
from starlette.concurrency import run_in_threadpool


def initialize_database() -> None:
    # Temporary until Global Chess migration/readiness ownership in Phase 6E.
    Base.metadata.create_all(bind=engine)


async def close_global_chess_resources() -> None:
    await run_in_threadpool(engine.dispose)


app = create_app(
    title="CivicStudio AI - Trend Scout",
    routers=(
        health.router,
        ingestion.router,
        topics.router,
        analysis.router,
        research.router,
    ),
    initialize_database=initialize_database,
    on_shutdown=close_global_chess_resources,
)
