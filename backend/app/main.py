
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.database import Base, engine
from app import models  # noqa: F401 — register SQLAlchemy models
from app.routes import analysis, health, ingestion, topics


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title="CivicStudio AI - Trend Scout",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(ingestion.router)
app.include_router(topics.router)
app.include_router(analysis.router)
