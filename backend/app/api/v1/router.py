from fastapi import APIRouter

from app.api.v1 import ai, articles, health, sources

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(health.router)
api_router.include_router(articles.router)
api_router.include_router(ai.router)
api_router.include_router(sources.router)