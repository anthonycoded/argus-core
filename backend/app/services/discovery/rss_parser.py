"""Bounded RSS 2.0/Atom parsing. Entry observations are never deduplicated."""

import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

from lxml import etree
from pydantic import ValidationError

from app.services.article_fetch import UnsafeDestination
from app.services.discovery.contracts import DiscoveryError
from app.services.discovery.feed_registry import ApprovedFeed
from app.services.discovery.google_news import candidate_url, _RejectExternalXML
from app.services.discovery.rss_contracts import RSSCandidate, RSSProvenance, RSSRejectedEntry

ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/elements/1.1/}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}"
XML_LIMIT = 1024 * 1024
PARSER_SECONDS = 2.0


def parse_rss(data: bytes, feed: ApprovedFeed) -> tuple[str | None, str, list, int]:
    if len(data) > XML_LIMIT:
        raise DiscoveryError("parser_limit")
    deadline = time.monotonic() + PARSER_SECONDS
    def check():
        if time.monotonic() >= deadline:
            raise DiscoveryError("parser_limit")
    parser = etree.XMLPullParser(events=("start", "end"), resolve_entities=False,
        load_dtd=False, no_network=True, huge_tree=False, recover=False,
        remove_comments=True, remove_pis=True)
    parser.resolvers.add(_RejectExternalXML())
    depth = nodes = count = 0
    try:
        for offset in range(0, len(data), 8192):
            check(); parser.feed(data[offset:offset + 8192])
            for event, element in parser.read_events():
                check()
                if event == "start":
                    depth += 1; nodes += 1
                    count += element.tag in {"item", ATOM + "entry"}
                    if depth > 32 or nodes > 10000 or count > 500:
                        raise DiscoveryError("parser_limit")
                else:
                    depth -= 1
        root = parser.close(); check()
    except etree.XMLSyntaxError:
        raise DiscoveryError("malformed_feed") from None
    if root.getroottree().docinfo.doctype:
        raise DiscoveryError("malformed_feed")
    if root.tag == "rss" and root.find("channel") is not None:
        container = root.find("channel"); namespace = ""; format_name = "rss2"
        entries = container.findall("item")
    elif root.tag == ATOM + "feed":
        container = root; namespace = ATOM; format_name = "atom"
        entries = container.findall(ATOM + "entry")
    else:
        raise DiscoveryError("malformed_feed")
    if count != len(entries):
        # Nested/multiple-channel entry containers must not silently hide items.
        raise DiscoveryError("malformed_feed")
    def text(element, tag):
        found = element.find(tag)
        return "" if found is None else "".join(found.itertext()).strip()
    feed_title = text(container, namespace + "title") or None
    if feed_title and len(feed_title) > 8192:
        raise DiscoveryError("parser_limit")
    observations = []; seen = set(); duplicates = 0
    for index, entry in enumerate(entries):
        check()
        title = text(entry, namespace + "title")
        if not title:
            observations.append(RSSRejectedEntry(entry_index=index, reason="missing_title")); continue
        if namespace:
            link = next((node.get("href", "") for node in entry.findall(ATOM + "link")
                         if node.get("rel", "alternate") == "alternate"), "")
            snippet = text(entry, ATOM + "summary") or text(entry, ATOM + "content") or None
            item_id = text(entry, ATOM + "id") or None
            date_field = "published" if text(entry, ATOM + "published") else "updated"
            raw_date = text(entry, ATOM + date_field) or None
        else:
            link = text(entry, "link")
            snippet = text(entry, "description") or text(entry, CONTENT + "encoded") or None
            item_id = text(entry, "guid") or None
            date_field = "pubDate" if text(entry, "pubDate") else "dc:date"
            raw_date = text(entry, "pubDate") or text(entry, DC + "date") or None
        reported_publisher = text(entry, "source") or text(entry, ATOM + "author/" + ATOM + "name") or None
        if any(len(value or "") > maximum for value, maximum in (
            (title, 8192), (snippet, 65536), (item_id, 2048), (raw_date, 512), (reported_publisher, 512),
        )):
            observations.append(RSSRejectedEntry(entry_index=index, reason="field_limit")); continue
        try:
            if not link or any(char.isspace() or ord(char) < 32 for char in link):
                raise ValueError
            # Resolve metadata only; xml:base never becomes a fetch destination.
            base = feed.url
            for ancestor in list(entry.iterancestors())[::-1] + [entry]:
                declared_base = ancestor.get("{http://www.w3.org/XML/1998/namespace}base")
                if declared_base:
                    base = urljoin(base, declared_base)
            url = candidate_url(urljoin(base, link))
            if len(link) > 2048:
                raise ValueError
        except (ValueError, UnsafeDestination):
            observations.append(RSSRejectedEntry(entry_index=index, reason="invalid_url")); continue
        published = None; warnings = []
        if raw_date:
            try:
                try:
                    published = parsedate_to_datetime(raw_date)
                except (TypeError, ValueError):
                    published = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
                    warnings.append("publication_timezone_assumed_utc")
                published = published.astimezone(timezone.utc)
            except (TypeError, ValueError, OverflowError):
                published = None; warnings.append("invalid_publication_date")
        try:
            candidate = RSSCandidate(title=title, url=url, canonical_url=url, domain=urlsplit(url).hostname or "", publisher=feed.label,
                published_at=published, snippet=snippet, provenance=RSSProvenance(
                    entry_index=index, original_url=link, provider_item_id=item_id,
                    published_raw=raw_date, date_field=date_field if raw_date else None, warnings=warnings,
                    reported_publisher=reported_publisher))
        except ValidationError:
            observations.append(RSSRejectedEntry(entry_index=index, reason="field_limit")); continue
        if url in seen:
            duplicates += 1
        seen.add(url); observations.append(candidate)
    check()
    return feed_title, format_name, observations, duplicates
