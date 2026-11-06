"""RSS-only fetch policy; Google News's fetch path remains unchanged."""

from dataclasses import dataclass
from typing import Protocol

import anyio
import httpx

from app.services.article_fetch import PublicHTTPTransport, UnsafeDestination
from app.services.discovery.contracts import DiscoveryError
from app.services.discovery.feed_registry import ApprovedFeed
from app.services.discovery.fetch import read_bounded_xml, retry_after_seconds

FETCH_DEADLINE = 20.0


@dataclass(frozen=True)
class FetchedRSS:
    data: bytes


class RSSFetcher(Protocol):
    async def fetch(self, feed: ApprovedFeed) -> FetchedRSS: ...


class SecureRSSFetcher:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.transport = transport

    async def fetch(self, feed: ApprovedFeed) -> FetchedRSS:
        try:
            with anyio.fail_after(FETCH_DEADLINE):
                transport = self.transport if self.transport is not None else PublicHTTPTransport()
                async with transport:
                    current = feed.approves(feed.url)
                    for hop in range(4):
                        request = httpx.Request("GET", current, headers={
                            "User-Agent": "ArgusCore/0.1", "Accept-Encoding": "identity",
                            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml",
                        }, extensions={"timeout": {"connect": 5.0, "read": 10.0, "write": 10.0, "pool": 2.0}})
                        response = await transport.handle_async_request(request)
                        try:
                            if response.status_code in {301, 302, 303, 307, 308}:
                                location = response.headers.get("location")
                                if not location or len(location) > 4096 or hop == 3:
                                    raise DiscoveryError("upstream_error")
                                current = feed.approves(current.join(location))
                                continue
                            if response.status_code == 429:
                                raise DiscoveryError("throttled", retry_after_seconds(response.headers.get("retry-after")))
                            if response.status_code != 200:
                                raise DiscoveryError("upstream_error", retry_after_seconds(response.headers.get("retry-after")))
                            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                            if content_type not in {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml"}:
                                raise DiscoveryError("malformed_feed")
                            return FetchedRSS(await read_bounded_xml(response))
                        finally:
                            with anyio.move_on_after(1, shield=True):
                                await response.aclose()
        except UnsafeDestination:
            raise DiscoveryError("unsafe_destination") from None
        except (httpx.InvalidURL, ValueError):
            raise DiscoveryError("unsafe_destination") from None
        except (TimeoutError, httpx.TimeoutException):
            raise DiscoveryError("timeout") from None
        except httpx.HTTPError:
            raise DiscoveryError("upstream_error") from None
        raise DiscoveryError("upstream_error")
