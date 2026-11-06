"""Bounded RSS retrieval over the existing DNS-pinned public HTTP transport."""

import math
import zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import anyio
import httpx

from app.services.article_fetch import PublicHTTPTransport, UnsafeDestination, validate_url
from app.services.discovery.contracts import DiscoveryError

GOOGLE_NEWS_ENDPOINT = "https://news.google.com/rss/search"
MAX_DOWNLOAD_BYTES = 1024 * 1024
MAX_XML_BYTES = 1024 * 1024
MAX_REDIRECTS = 3
FETCH_DEADLINE = 20.0


def retry_after_seconds(value: str | None) -> str | None:
    """Sanitized guidance, at most one day; never causes a sleep or retry."""
    if not value or len(value) > 128:
        return None
    value = value.strip()
    if value.isascii() and value.isdigit() and len(value) <= 10:
        return str(min(int(value), 86400))
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return None
        return str(min(86400, max(0, math.ceil((date - datetime.now(timezone.utc)).total_seconds()))))
    except (TypeError, ValueError, OverflowError):
        return None


def approved_url(value: str | httpx.URL) -> httpx.URL:
    url = validate_url(value)
    if url.scheme != "https" or url.host != "news.google.com" or url.port not in {None, 443} or url.fragment:
        raise DiscoveryError("unsafe_destination")
    return url


async def read_bounded_xml(response: httpx.Response) -> bytes:
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise DiscoveryError("unsupported_encoding")
    length = response.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdigit() or len(length) > 20:
            raise DiscoveryError("malformed_feed")
        if int(length) > MAX_DOWNLOAD_BYTES:
            raise DiscoveryError("response_too_large")
    decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding != "identity" else None
    output = bytearray()
    downloaded = 0
    try:
        async for chunk in response.aiter_raw(chunk_size=65536):
            downloaded += len(chunk)
            if downloaded > MAX_DOWNLOAD_BYTES:
                raise DiscoveryError("response_too_large")
            if decoder is not None:
                chunk = decoder.decompress(chunk, MAX_XML_BYTES - len(output) + 1)
                if decoder.unconsumed_tail:
                    raise DiscoveryError("response_too_large")
                if decoder.unused_data:
                    raise DiscoveryError("malformed_feed")
            if len(output) + len(chunk) > MAX_XML_BYTES:
                raise DiscoveryError("response_too_large")
            output.extend(chunk)
        if decoder is not None and not decoder.eof:
            raise DiscoveryError("malformed_feed")
        if length is not None and downloaded != int(length):
            raise DiscoveryError("malformed_feed")
    except zlib.error:
        raise DiscoveryError("malformed_feed") from None
    return bytes(output)


class SecureDiscoveryFetcher:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None):
        # Transport injection is a trusted offline-testing seam, never an API option.
        self._transport = transport

    async def fetch_google_news(self, query: str) -> bytes:
        current = httpx.URL(GOOGLE_NEWS_ENDPOINT, params={
            "q": query, "hl": "en-US", "gl": "US", "ceid": "US:en",
        })
        try:
            with anyio.fail_after(FETCH_DEADLINE):
                # Use HTTPX's public transport interface directly: no proxy,
                # cookie, or credential inheritance, and no high-level HTTPX
                # request log containing the sensitive search query URL.
                transport = self._transport if self._transport is not None else PublicHTTPTransport()
                async with transport:
                    for hop in range(MAX_REDIRECTS + 1):
                        current = approved_url(current)
                        request = httpx.Request(
                            "GET", current,
                            headers={"User-Agent": "ArgusCore/0.1", "Accept-Encoding": "identity",
                                     "Accept": "application/rss+xml, application/xml, text/xml"},
                            extensions={"timeout": {"connect": 5.0, "read": 10.0, "write": 10.0, "pool": 2.0}},
                        )
                        response = await transport.handle_async_request(request)
                        try:
                            if response.status_code in {301, 302, 303, 307, 308}:
                                location = response.headers.get("location")
                                if not location or len(location) > 4096 or hop == MAX_REDIRECTS:
                                    raise DiscoveryError("upstream_error")
                                current = approved_url(current.join(location))
                                continue
                            if response.status_code == 429:
                                raise DiscoveryError("throttled", retry_after_seconds(response.headers.get("retry-after")))
                            if response.status_code != 200:
                                raise DiscoveryError("upstream_error", retry_after_seconds(response.headers.get("retry-after")))
                            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                            if content_type not in {"application/rss+xml", "application/xml", "text/xml"}:
                                raise DiscoveryError("malformed_feed")
                            return await read_bounded_xml(response)
                        finally:
                            with anyio.move_on_after(1, shield=True):
                                await response.aclose()
        except UnsafeDestination:
            raise DiscoveryError("unsafe_destination") from None
        except (TimeoutError, httpx.TimeoutException):
            raise DiscoveryError("timeout") from None
        except (httpx.InvalidURL, ValueError):
            raise DiscoveryError("unsafe_destination") from None
        except httpx.HTTPError:
            raise DiscoveryError("upstream_error") from None
        raise DiscoveryError("upstream_error")
