"""Fixed-origin, nonredirecting, bounded JSON retrieval over pinned TLS."""

import re
import zlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Protocol

import anyio
import httpx

from app.services.article_fetch import PublicHTTPTransport, UnsafeDestination, validate_url
from app.services.discovery.gdelt_contracts import GDELTFault, GDELTRequest, RetryPolicy

ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_BYTES = 1024 * 1024
FETCH_DEADLINE = 20.0


def retry_policy(value: str | None, fallback: int) -> RetryPolicy:
    if not value or len(value) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        return RetryPolicy(delay_seconds=fallback)
    value = value.strip()
    if value.isascii() and value.isdigit():
        if len(value) > 10 or int(value) > 2147483647:
            return RetryPolicy(hold=True)
        return RetryPolicy(delay_seconds=int(value))
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            raise ValueError
        epoch = datetime(1970,1,1,tzinfo=timezone.utc)
        until = int((date.astimezone(timezone.utc)-epoch).total_seconds()*1000)
        # Oversized valid guidance pauses admission rather than shortening it.
        if until > 9000000000000:
            return RetryPolicy(hold=True)
        return RetryPolicy(until_ms=max(0,until))
    except (TypeError, ValueError, OverflowError):
        return RetryPolicy(delay_seconds=fallback)


async def read_json_bytes(response: httpx.Response) -> bytes:
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise GDELTFault("unsupported_encoding")
    length = response.headers.get("content-length")
    if length is not None:
        if not re.fullmatch(r"[0-9]{1,20}", length):
            raise GDELTFault("malformed_response")
        if int(length) > MAX_BYTES:
            raise GDELTFault("response_too_large")
    decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding != "identity" else None
    body = bytearray(); downloaded = 0
    try:
        async for chunk in response.aiter_raw(chunk_size=65536):
            downloaded += len(chunk)
            if downloaded > MAX_BYTES:
                raise GDELTFault("response_too_large")
            if decoder:
                chunk = decoder.decompress(chunk, MAX_BYTES-len(body)+1)
                if decoder.unconsumed_tail:
                    raise GDELTFault("response_too_large")
                if decoder.unused_data:
                    raise GDELTFault("malformed_response")
            if len(body)+len(chunk) > MAX_BYTES:
                raise GDELTFault("response_too_large")
            body.extend(chunk)
        if (decoder and not decoder.eof) or (length is not None and downloaded != int(length)):
            raise GDELTFault("malformed_response")
    except zlib.error:
        raise GDELTFault("malformed_response") from None
    return bytes(body)


class GDELTFetcher(Protocol):
    async def fetch(self, request: GDELTRequest) -> bytes: ...


class SecureGDELTFetcher:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport

    async def fetch(self, request: GDELTRequest) -> bytes:
        try:
            with anyio.fail_after(FETCH_DEADLINE):
                url = validate_url(httpx.URL(ENDPOINT, params={
                    "query":request.query, "mode":"ArtList", "format":"json",
                    "maxrecords":request.limit, "sort":"HybridRel", "timespan":f"{request.window_hours}h",
                }))
                if (url.scheme != "https" or url.host != "api.gdeltproject.org" or url.port not in {None,443}
                        or url.path != "/api/v2/doc/doc" or url.fragment):
                    raise GDELTFault("unsafe_destination")
                transport = self.transport if self.transport is not None else PublicHTTPTransport()
                async with transport:
                    outbound = httpx.Request("GET", url, headers={"User-Agent":"ArgusCore/0.1",
                        "Accept":"application/json", "Accept-Encoding":"identity"},
                        extensions={"timeout":{"connect":5.0,"read":10.0,"write":10.0,"pool":2.0}})
                    response = await transport.handle_async_request(outbound)
                    try:
                        if response.status_code == 429:
                            raise GDELTFault("throttled", policy=retry_policy(response.headers.get("retry-after"),300))
                        if response.status_code != 200:
                            raise GDELTFault("upstream_error", policy=retry_policy(response.headers.get("retry-after"),6))
                        content_type = response.headers.get("content-type", "").split(";",1)[0].strip().lower()
                        if content_type != "application/json":
                            raise GDELTFault("malformed_response")
                        return await read_json_bytes(response)
                    finally:
                        with anyio.move_on_after(1, shield=True):
                            await response.aclose()
        except UnsafeDestination:
            raise GDELTFault("unsafe_destination") from None
        except (TimeoutError, httpx.TimeoutException):
            raise GDELTFault("timeout") from None
        except httpx.HTTPError:
            raise GDELTFault("upstream_error") from None
        except (httpx.InvalidURL, ValueError):
            raise GDELTFault("unsafe_destination") from None
