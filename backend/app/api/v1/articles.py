import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, HttpUrl

from app.core.permissions import require_scopes
from app.core.rate_limit import require_rate_limit

from app.services.article_extraction import extract_articles
from app.services.rate_limiter import (
    GENERAL_API_LIMIT,
    ARTICLE_EXTRACTION_LIMIT,
)

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
    _auth=Depends(require_scopes("articles:extract")),
    _general_limit=Depends(
        require_rate_limit(
            bucket="general",
            rate_limit=GENERAL_API_LIMIT,
        )
    ),
    _extraction_limit=Depends(
        require_rate_limit(
            bucket="article_extraction",
            rate_limit=ARTICLE_EXTRACTION_LIMIT,
        )
    ),
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