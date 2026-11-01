
import asyncio
import logging

import httpx
import trafilatura

logger = logging.getLogger(__name__)

USER_AGENT = "ArgusCore/0.1"

REQUEST_TIMEOUT = 20.0
MAX_CONCURRENT_EXTRACTIONS = 5
MAX_ARTICLE_LENGTH = 20000


async def extract_article(
    url: str,
    client: httpx.AsyncClient,
) -> dict:
    """
    Retrieve an article and extract its main text.

    Returns extraction metadata without discarding the
    original source record.
    """

    result = {
        "extraction_status": "failed",
        "article_text": None,
        "extraction_error": None,
    }

    if not url or not url.startswith(("https://", "http://")):
        result["extraction_error"] = "Invalid URL"
        return result

    try:
        response = await client.get(url)
        response.raise_for_status()

        content_type = response.headers.get(
            "content-type", ""
        ).lower()

        if "html" not in content_type:
            result["extraction_status"] = "unsupported_content"
            result["extraction_error"] = (
                f"Unsupported content type: {content_type}"
            )
            return result

        article_text = await asyncio.to_thread(
            trafilatura.extract,
            response.text,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )

        if not article_text:
            result["extraction_status"] = "no_content"
            result["extraction_error"] = (
                "No extractable article text found."
            )
            return result

        article_text = article_text.strip()

        if len(article_text) < 200:
            result["extraction_status"] = "insufficient_content"
            result["extraction_error"] = (
                "Extracted text is too short."
            )
            return result

        result["extraction_status"] = "success"
        result["article_text"] = article_text[:MAX_ARTICLE_LENGTH]

        return result

    except httpx.HTTPStatusError as exc:
        result["extraction_status"] = "http_error"
        result["extraction_error"] = (
            f"HTTP {exc.response.status_code}"
        )

        logger.warning(
            "Article retrieval returned HTTP %s: %s",
            exc.response.status_code,
            url,
        )

        return result

    except httpx.TimeoutException:
        result["extraction_status"] = "timeout"
        result["extraction_error"] = "Request timed out."
        return result

    except httpx.HTTPError as exc:
        result["extraction_status"] = "request_error"
        result["extraction_error"] = str(exc)
        return result

    except Exception:
        logger.exception(
            "Article extraction failed: %s",
            url,
        )

        result["extraction_status"] = "failed"
        result["extraction_error"] = "Unexpected extraction error."

        return result


async def extract_articles(
    sources: list[dict],
) -> list[dict]:
    """
    Enrich discovered sources with extracted article text.
    """

    semaphore = asyncio.Semaphore(
        MAX_CONCURRENT_EXTRACTIONS
    )

    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    ) as client:

        async def process_source(source: dict) -> dict:
            async with semaphore:
                extraction = await extract_article(
                    source.get("url", ""),
                    client,
                )

                return {
                    **source,
                    **extraction,
                }

        return await asyncio.gather(
            *[
                process_source(source)
                for source in sources
            ]
        )
