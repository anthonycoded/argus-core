from app.global_chess_factory import create_app

# Register Global Chess database models.
from app import models  # noqa: F401

from app.database import engine
from app.global_chess_readiness import GlobalChessReadinessError, MIGRATION_HINT, validate_global_chess_database
from app.global_chess_config import global_chess_settings
from app.routes import analysis, health, ingestion, research, topics
from starlette.concurrency import run_in_threadpool


async def check_global_chess_database() -> None:
    if global_chess_settings.database_url is None:
        raise GlobalChessReadinessError(
            "Global Chess startup requires explicit GLOBAL_CHESS_DATABASE_URL; legacy compatibility cannot select a serving database. "
            + MIGRATION_HINT
        )
    await run_in_threadpool(validate_global_chess_database, engine)


async def close_global_chess_resources() -> None:
    await run_in_threadpool(engine.dispose)


app = create_app(
    routers=(
        health.router,
        ingestion.router,
        topics.router,
        analysis.router,
        research.router,
    ),
    check_database=check_global_chess_database,
    on_shutdown=close_global_chess_resources,
)
