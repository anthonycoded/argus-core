from app.factory import create_app

# Register Global Chess database models.
from app import models  # noqa: F401

from app.core.database import Base, engine
from app.routes import analysis, health, ingestion, research, topics


def initialize_database() -> None:
    Base.metadata.create_all(bind=engine)


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
)
