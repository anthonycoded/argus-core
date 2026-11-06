
import asyncio
import logging
import zlib

import httpx
import trafilatura

from app.services.article_fetch import PublicHTTPTransport, validate_url

logger = logging.getLogger(__name__)

USER_AGENT = "ArgusCore/0.1"

REQUEST_TIMEOUT = 20.0
MAX_CONCURRENT_EXTRACTIONS = 5
MAX_ARTICLE_LENGTH = 20000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5


async def download_article(url: str, client: httpx.AsyncClient) -> httpx.Response:
    """Validate every redirect and bound the body before parsing any HTML."""
    current = validate_url(url)
    for hop in range(MAX_REDIRECTS + 1):
        async with client.stream("GET", current, follow_redirects=False) as response:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    response.raise_for_status()
                if hop == MAX_REDIRECTS:
                    raise httpx.RequestError("Too many article redirects.")
                current = validate_url(current.join(location))
                continue
            response.raise_for_status()
            if "html" not in response.headers.get("content-type", "").lower():
                return response
            # Read raw chunks so HTTPX cannot allocate an unbounded decompressed
            # chunk. Decode common encodings separately with an output bound.
            encoding = response.headers.get("content-encoding", "identity").strip().lower()
            if encoding not in {"identity", "gzip", "deflate"}:
                raise httpx.RequestError("Unsupported article content encoding.")
            length = response.headers.get("content-length")
            if length is not None:
                try:
                    declared_size = int(length)
                except ValueError as exc:
                    raise httpx.RequestError("Invalid article content length.") from exc
                if declared_size < 0 or declared_size > MAX_RESPONSE_BYTES:
                    raise httpx.RequestError("Article response exceeds download limit.")
            body = bytearray()
            async for chunk in response.aiter_raw():
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise httpx.RequestError("Article response exceeds download limit.")
                body.extend(chunk)
            if encoding != "identity":
                try:
                    decoder = zlib.decompressobj(31 if encoding == "gzip" else 15)
                    body = decoder.decompress(bytes(body), MAX_RESPONSE_BYTES + 1)
                except zlib.error as exc:
                    raise httpx.RequestError("Invalid compressed article response.") from exc
                if len(body) > MAX_RESPONSE_BYTES or decoder.unconsumed_tail:
                    raise httpx.RequestError("Article response exceeds download limit.")
                if not decoder.eof or decoder.unused_data:
                    raise httpx.RequestError("Invalid compressed article response.")
            headers = dict(response.headers)
            headers.pop("content-encoding", None)
            headers.pop("content-length", None)
            return httpx.Response(
                response.status_code, headers=headers,
                content=bytes(body), request=response.request,
            )
    raise httpx.RequestError("Too many article redirects.")


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
        # HTTPX timeouts are per network operation. Also bound the complete
        # download, including DNS, redirects and slow trickle responses.
        async with asyncio.timeout(REQUEST_TIMEOUT):
            response = await download_article(url, client)
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

    except (httpx.TimeoutException, TimeoutError):
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
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
        follow_redirects=False,
        trust_env=False,
        transport=PublicHTTPTransport(),
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
