"""Google News RSS candidates, with no editorial filters or database access."""

import time
import re
from urllib.parse import urlsplit

import anyio
from lxml import etree
from pydantic import HttpUrl, TypeAdapter, ValidationError

from app.services.article_fetch import UnsafeDestination, validate_url
from app.services.discovery.contracts import (
    CandidateProvenance, DiscoveryCandidate, DiscoveryError, DiscoveryFetcher,
    DiscoveryProviderResult, DiscoveryRequest,
)
from app.services.discovery.fetch import MAX_XML_BYTES, SecureDiscoveryFetcher
from app.utils.source_normalization import normalize_url, parse_published

MAX_XML_NODES = 10000
MAX_XML_DEPTH = 32
MAX_FEED_ITEMS = 500
PARSER_DEADLINE = 2.0
OPERATION_DEADLINE = 25.0
# Bounds in-flight fetches/parsers in this worker; shared app/key quotas use Redis.
_capacity = anyio.CapacityLimiter(5)
_http_url = TypeAdapter(HttpUrl)


class _RejectExternalXML(etree.Resolver):
    def resolve(self, url, public_id, context):
        # no_network alone does not prohibit file:// external entities.
        raise DiscoveryError("malformed_feed")


def candidate_url(value: str) -> str:
    """Validate links without fetching them; future dereferences must check DNS."""
    if not value or len(value) > 2048 or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Invalid candidate URL")
    if "\\" in value:
        raise ValueError("Invalid candidate URL")
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ValueError("Invalid candidate URL")
    raw = urlsplit(value)
    if raw.scheme.lower() not in {"https", "http"} or not raw.netloc:
        raise ValueError("Invalid candidate URL")
    parsed = _http_url.validate_python(value)
    validate_url(str(parsed))
    normalized = normalize_url(str(parsed))
    if len(normalized) > 2048:
        raise ValueError("Invalid candidate URL")
    host = parsed.host or ""
    if len(host) > 253 or not all(
        re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
        for label in host.rstrip(".").split(".")
    ):
        # IPv6 literals are already validated by HttpUrl/validate_url.
        if ":" not in host:
            raise ValueError("Invalid candidate URL")
    return normalized


def parse_google_news(data: bytes, limit: int) -> DiscoveryProviderResult:
    if len(data) > MAX_XML_BYTES:
        raise DiscoveryError("parser_limit")
    deadline = time.monotonic() + PARSER_DEADLINE
    def check_deadline():
        if time.monotonic() >= deadline:
            raise DiscoveryError("parser_limit")
    parser = etree.XMLPullParser(
        events=("start", "end"), resolve_entities=False, load_dtd=False,
        no_network=True, huge_tree=False, recover=False, remove_comments=True, remove_pis=True,
    )
    parser.resolvers.add(_RejectExternalXML())
    depth = nodes = items = 0
    try:
        for offset in range(0, len(data), 8192):
            check_deadline()
            parser.feed(data[offset:offset + 8192])
            for event, element in parser.read_events():
                check_deadline()
                if event == "start":
                    depth += 1
                    nodes += 1
                    items += element.tag == "item"
                    if depth > MAX_XML_DEPTH or nodes > MAX_XML_NODES or items > MAX_FEED_ITEMS:
                        raise DiscoveryError("parser_limit")
                else:
                    depth -= 1
        root = parser.close()
        check_deadline()
    except etree.XMLSyntaxError:
        raise DiscoveryError("malformed_feed") from None
    # No DTD/entity declarations, including UTF-16/other XML encodings.
    if root.getroottree().docinfo.doctype or root.tag != "rss":
        raise DiscoveryError("malformed_feed")
    channel = root.find("channel")
    if channel is None:
        raise DiscoveryError("malformed_feed")
    result = DiscoveryProviderResult()
    seen = set()
    for item in channel.findall("item"):
        check_deadline()
        def text(name):
            element = item.find(name)
            return "" if element is None else "".join(element.itertext()).strip()
        try:
            title, original = text("title"), text("link")
            if not title:
                raise ValueError("Missing title")
            url = candidate_url(original)
            publisher = text("source") or "Google News"
            snippet = text("description") or text("summary") or None
            published = text("pubDate") or None
            source = item.find("source")
            publisher_url = source.get("url") if source is not None else None
            if publisher_url:
                try:
                    publisher_url = candidate_url(publisher_url)
                except (ValueError, UnsafeDestination):
                    publisher_url = None
            truncated_fields = [name for name, value, maximum in (
                ("title", title, 1000), ("publisher", publisher, 255), ("snippet", snippet or "", 16384),
            ) if len(value) > maximum]
            candidate = DiscoveryCandidate(
                title=title[:1000], url=url, canonical_url=url,
                domain=urlsplit(url).hostname or "", publisher=publisher[:255],
                snippet=snippet[:16384] if snippet else None,
                published_at=parse_published({"published": published}),
                provenance=CandidateProvenance(
                    original_url=original, provider_item_id=text("guid") or None,
                    published_raw=published, truncated_fields=truncated_fields,
                    publisher_url=publisher_url,
                ),
            )
        except (ValueError, TypeError, OverflowError, UnsafeDestination, ValidationError):
            result.invalid_candidates += 1
            continue
        if url in seen:
            result.duplicates += 1
            continue
        seen.add(url)
        if len(result.candidates) < limit:
            result.candidates.append(candidate)
        else:
            result.truncated = True
    return result


class GoogleNewsProvider:
    def __init__(self, fetcher: DiscoveryFetcher | None = None):
        self._fetcher = fetcher if fetcher is not None else SecureDiscoveryFetcher()

    async def discover(self, request: DiscoveryRequest) -> DiscoveryProviderResult:
        try:
            with anyio.fail_after(OPERATION_DEADLINE):
                async with _capacity:
                    data = await self._fetcher.fetch_google_news(request.query)
                    # Parser has its own byte/structure/time bounds. Do not abandon
                    # a running parser thread and free capacity while it still runs.
                    result = await anyio.to_thread.run_sync(parse_google_news, data, request.limit)
                    await anyio.lowlevel.checkpoint()
                    return result
        except DiscoveryError as exc:
            return exc.result()
        except TimeoutError:
            return DiscoveryError("timeout").result()
