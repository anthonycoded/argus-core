"""Approved public feeds, owned by deployment configuration, never request data."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

import httpx

from app.services.article_fetch import validate_url


@dataclass(frozen=True)
class ApprovedFeed:
    feed_id: str
    url: str
    label: str
    enabled: bool = True
    revision: str = "1"

    def approves(self, value: str | httpx.URL) -> httpx.URL:
        candidate = validate_url(value)
        expected = httpx.URL(self.url)
        if (candidate.scheme != "https" or candidate.host != expected.host
                or candidate.port not in {None, 443} or candidate.fragment):
            raise ValueError("Unapproved feed destination")
        return candidate


APPROVED_FEEDS: Mapping[str, ApprovedFeed] = MappingProxyType({
    "bbc-world": ApprovedFeed("bbc-world", "https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World"),
    "npr-politics": ApprovedFeed("npr-politics", "https://feeds.npr.org/1014/rss.xml", "NPR Politics"),
})


def approved_feed(feed_id: str) -> ApprovedFeed | None:
    feed = APPROVED_FEEDS.get(feed_id)
    return feed if feed is not None and feed.enabled else None
