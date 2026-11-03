import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, HttpUrl

from app.services.article_extraction import extract_articles

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/articles",
    tags=["Articles"],
)


class ArticleExtractionRequest(BaseModel):
    url: HttpUrl


@router.post("/extract")
async def extract_article_endpoint(
    request: ArticleExtractionRequest,
):
    try:
        results = await extract_articles(
            [{"url": str(request.url)}]
        )

        if not results:
            raise HTTPException(
                status_code=500,
                detail="Article extraction returned no results.",
            )

        return results[0]

    except HTTPException:
        raise

    except Exception:
        logger.exception("Article extraction endpoint failed")

        raise HTTPException(
            status_code=500,
            detail="Article extraction service unavailable.",
        )