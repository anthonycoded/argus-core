from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from xml.sax.saxutils import escape

import httpcore
import pytest
from pydantic import ValidationError

from app.services.discovery.contracts import DiscoveryRequest, DiscoveryError
from app.services.discovery.google_news import GoogleNewsProvider, parse_google_news
from app.services.discovery import google_news

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", AsyncMock(
        side_effect=AssertionError("Unexpected live discovery HTTP")))


@pytest.mark.asyncio
async def test_normalized_candidates_and_provenance():
    fetcher = AsyncMock()
    fetcher.fetch_google_news.return_value = (FIXTURES / "google_news.xml").read_bytes()
    result = await GoogleNewsProvider(fetcher).discover(DiscoveryRequest(query="research topic"))
    assert result.status == "success" and len(result.candidates) == 2
    assert result.duplicates == 1 and result.invalid_candidates == 1
    first, second = result.candidates
    assert first.provider == first.source_type == "google_news"
    assert first.publisher == "Example News"
    assert first.published_at == datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    assert first.canonical_url == first.url == "https://news.google.com/rss/articles/fixture-one"
    assert first.provenance.provider_item_id == "fixture-one"
    assert first.provenance.publisher_url == "https://example.com/"
    assert first.provenance.original_url.endswith("?utm_source=test")
    assert "A supplied snippet." in first.snippet
    assert second.published_at is None and second.publisher == "Google News"
    assert second.url == "https://example.com/another"
    fetcher.fetch_google_news.assert_awaited_once_with("research topic")


@pytest.mark.parametrize("data", [b"", b"not XML", b"<rss><channel>", b"<html/>", b"<rss/>",
    b'<!DOCTYPE rss [<!ENTITY x "secret">]><rss><channel><title>&x;</title></channel></rss>',
    b'<!DOCTYPE rss SYSTEM "file:///must-not-read"><rss><channel/></rss>',
    '<!DOCTYPE rss [<!ENTITY x SYSTEM "https://must-not-contact.test/">]><rss><channel/></rss>'.encode('utf-16'),
])
def test_malformed_and_entity_feeds_are_rejected(data):
    with pytest.raises(DiscoveryError) as error:
        parse_google_news(data, 10)
    assert error.value.status == "malformed_feed"


@pytest.mark.parametrize("payload", [
    {"query": ""}, {"query": "   "}, {"query": "a\nb"}, {"query": "x" * 1001},
    {"query": "topic", "limit": 0}, {"query": "topic", "limit": 76},
    {"query": "topic", "limit": True}, {"query": "topic", "limit": "10"},
    {"query": "topic", "provider": "rss"}, {"query": "topic", "url": "http://localhost"},
])
def test_invalid_request_contract(payload):
    with pytest.raises(ValidationError):
        DiscoveryRequest.model_validate(payload)


def test_empty_result_is_success_and_limit_is_explicit():
    empty = parse_google_news((FIXTURES / "empty.xml").read_bytes(), 10)
    assert empty.status == "success" and empty.candidates == [] and empty.error is None
    limited = parse_google_news((FIXTURES / "google_news.xml").read_bytes(), 1)
    assert len(limited.candidates) == 1 and limited.truncated


@pytest.mark.parametrize("url", [
    "http://localhost/a", "http://127.1/a", "http://2130706433/a", "http://10.0.0.1/",
    "http://[::1]/", "http://metadata.internal/", "file:///etc/passwd", "ftp://example.com/",
    "https://user:password@example.com/", "https://example.com/" + "x" * 2048,
    "https://example.com/%zz", "https://example.com/a b", "https://bad_host.test/",
    "https://example.com:bad/a", "https://example.com\\@localhost/", "//example.com/path",
])
def test_invalid_candidate_urls_are_dropped_never_truncated(url):
    data = f'<rss><channel><item><title>Title</title><link>{escape(url)}</link></item></channel></rss>'.encode()
    result = parse_google_news(data, 10)
    assert result.candidates == [] and result.invalid_candidates == 1


def test_invalid_dates_are_null_and_text_truncation_is_explicit():
    data = ('<rss><channel><item><title>' + 'x' * 1001 + '</title>'
            '<link>https://example.com/report</link><pubDate>invalid</pubDate>'
            '<source url="http://localhost/">Publisher</source></item></channel></rss>').encode()
    candidate, = parse_google_news(data, 10).candidates
    assert candidate.published_at is None and len(candidate.title) == 1000
    assert candidate.provenance.truncated_fields == ["title"]
    assert candidate.provenance.publisher_url is None


@pytest.mark.parametrize("kind", ["bytes", "nodes", "depth", "items", "time"])
def test_parser_resource_limits(kind, monkeypatch):
    if kind == "bytes":
        data = b"x" * (google_news.MAX_XML_BYTES + 1)
    elif kind == "depth":
        data = b"<rss><channel>" + b"<a>" * 33 + b"</a>" * 33 + b"</channel></rss>"
    elif kind == "items":
        data = b"<rss><channel>" + b"<item/>" * 501 + b"</channel></rss>"
    elif kind == "nodes":
        data = b"<rss><channel>" + b"<a/>" * 10001 + b"</channel></rss>"
    else:
        monkeypatch.setattr(google_news, "PARSER_DEADLINE", 0)
        data = b"<rss><channel/></rss>"
    with pytest.raises(DiscoveryError) as error:
        parse_google_news(data, 10)
    assert error.value.status == "parser_limit"


@pytest.mark.asyncio
async def test_operation_deadline(monkeypatch):
    import anyio
    async def wait(query):
        await anyio.sleep_forever()
    monkeypatch.setattr(google_news, "OPERATION_DEADLINE", 0.01)
    fetcher = AsyncMock()
    fetcher.fetch_google_news.side_effect = wait
    result = await GoogleNewsProvider(fetcher).discover(DiscoveryRequest(query="topic"))
    assert result.status == "timeout"
    assert google_news._capacity.borrowed_tokens == 0


@pytest.mark.asyncio
async def test_parser_is_not_abandoned_on_operation_timeout(monkeypatch):
    import time
    from threading import Event
    from app.services.discovery.contracts import DiscoveryProviderResult
    finished = Event()
    def parser(data, limit):
        time.sleep(0.05)
        finished.set()
        return DiscoveryProviderResult()
    monkeypatch.setattr(google_news, "parse_google_news", parser)
    monkeypatch.setattr(google_news, "OPERATION_DEADLINE", 0.01)
    fetcher = AsyncMock()
    fetcher.fetch_google_news.return_value = b"<rss><channel/></rss>"
    result = await GoogleNewsProvider(fetcher).discover(DiscoveryRequest(query="topic"))
    assert result.status == "timeout" and finished.is_set()
    assert google_news._capacity.borrowed_tokens == 0
