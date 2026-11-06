"""Global Chess discovery contract tests; never contact a live provider."""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpcore
import httpx
import pytest

from app.clients.argus_core_client import ArgusCoreClient, CoreClientError
from app.services.research_service import discover_google_news_sources

FIXTURE = Path(__file__).parent / "fixtures" / "discovery" / "core_google_news.json"
KEY = "test-only-discovery-key"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", AsyncMock(
        side_effect=AssertionError("Unexpected live Core/provider HTTP")))
    monkeypatch.setattr("app.services.research_service.feedparser.parse", Mock(
        side_effect=AssertionError("Google News discovery parsed/fetched a feed locally")))


@pytest.fixture
def envelope():
    return json.loads(FIXTURE.read_text())


@pytest.mark.asyncio
async def test_google_news_discovers_articles(envelope):
    requests = []
    def respond(request):
        requests.append(request)
        assert request.method == "POST" and request.url == "https://core.test/prefix/api/v1/discovery"
        assert request.headers["X-API-Key"] == KEY
        assert json.loads(request.content) == {"provider": "google_news", "query": "Topic & context", "limit": 30}
        assert all(value == 12.5 for value in request.extensions["timeout"].values())
        return httpx.Response(200, json=envelope)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = ArgusCoreClient(base_url="https://core.test/prefix/", api_key=KEY, request_timeout=12.5, http_client=http)
        sources = await discover_google_news_sources("Topic & context", 30, discovery_client=client)
        assert not http.is_closed
    source, = sources
    assert source["title"] == envelope["candidates"][0]["title"]
    assert source["url"] == envelope["candidates"][0]["url"]
    assert source["publisher"] == "Example News" and source["source_type"] == "google_news"
    assert source["published_at"] == datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    assert source["snippet"] == "Only supplied source information."
    assert source["provider_metadata"]["provenance"] == envelope["candidates"][0]["provenance"]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_successful_empty_results_are_not_failures(envelope):
    envelope["candidates"] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=envelope))) as http:
        result = await discover_google_news_sources("Topic", discovery_client=ArgusCoreClient(
            base_url="https://core.test", api_key=KEY, http_client=http))
    assert result == []


@pytest.mark.asyncio
@pytest.mark.parametrize("query,limit", [("", 10), ("   ", 10), ("a\nb", 10), ("x" * 1001, 10),
                                         ("Topic", 0), ("Topic", 76), ("Topic", True)])
async def test_invalid_queries_and_limits_fail_before_request(query, limit):
    http = Mock()
    with pytest.raises(CoreClientError) as error:
        await discover_google_news_sources(query, limit, discovery_client=ArgusCoreClient(
            base_url="https://core.test", api_key=KEY, http_client=http))
    assert error.value.status_code == 422
    http.post.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(401, 502), (403, 502), (429, 429), (503, 503),
                                           (504, 504), (502, 502), (422, 502), (302, 502)])
async def test_failures_are_explicit_sanitized_and_not_retried(status, expected, caplog):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(status, headers={"Retry-After": "60", "Location": "https://news.google.com"},
                              json={"detail": KEY, "error": "private provider diagnostics"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as http:
        with pytest.raises(CoreClientError) as error:
            await discover_google_news_sources("Topic", discovery_client=ArgusCoreClient(
                base_url="https://core.test", api_key=KEY, http_client=http))
    assert error.value.status_code == expected
    assert error.value.retry_after == ("60" if status in {429, 503, 502, 422, 302} else None)
    assert len(requests) == 1 and KEY not in str(error.value) + caplog.text
    assert "private" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,expected", [(httpx.ReadTimeout, 504), (httpx.ConnectTimeout, 504),
                                           (httpx.ConnectError, 502), (httpx.RemoteProtocolError, 502)])
async def test_timeouts_and_network_failures(fault, expected):
    requests = []
    def respond(request):
        requests.append(request)
        raise fault("private URL or credentials", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(CoreClientError) as error:
            await discover_google_news_sources("Topic", discovery_client=ArgusCoreClient(
                base_url="https://core.test", api_key=KEY, http_client=http))
    assert error.value.status_code == expected and len(requests) == 1
    assert "private" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["no_status", "error_status", "no_candidates", "wrong_provider", "bad_title",
    "bad_url", "url_credentials", "private_url", "long_url", "no_provenance", "invalid_date", "naive_date",
    "numeric_date", "wrong_source_type", "bad_counter", "too_many", "extra_error"])
async def test_malformed_envelopes_and_candidates_fail_closed(envelope, fault):
    row = envelope["candidates"][0]
    if fault == "no_status": envelope.pop("status")
    elif fault == "error_status": envelope["status"] = "timeout"
    elif fault == "no_candidates": envelope.pop("candidates")
    elif fault == "wrong_provider": envelope["provider"] = "rss"
    elif fault == "bad_title": row["title"] = 123
    elif fault == "bad_url": row["url"] = "not a URL"
    elif fault == "url_credentials": row["url"] = "https://user:password@example.com/"
    elif fault == "private_url": row["url"] = "http://127.1/"
    elif fault == "long_url": row["url"] = "https://example.com/" + "x" * 2048
    elif fault == "no_provenance": row.pop("provenance")
    elif fault == "invalid_date": row["published_at"] = "invalid"
    elif fault == "naive_date": row["published_at"] = "2026-11-06T12:00:00"
    elif fault == "numeric_date": row["published_at"] = 1000
    elif fault == "wrong_source_type": row["source_type"] = "rss"
    elif fault == "bad_counter": envelope["duplicates"] = -1
    elif fault == "too_many": envelope["candidates"] = [copy.deepcopy(row) for _ in range(11)]
    else: envelope["error"] = "private error"
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=envelope))) as http:
        with pytest.raises(CoreClientError, match="invalid discovery response") as error:
            await discover_google_news_sources("Topic", discovery_client=ArgusCoreClient(
                base_url="https://core.test", api_key=KEY, http_client=http))
    assert error.value.status_code == 502


@pytest.mark.asyncio
async def test_invalid_json_and_retry_after_are_sanitized():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=KEY))) as http:
        with pytest.raises(CoreClientError, match="invalid discovery response"):
            await ArgusCoreClient(base_url="https://core.test", api_key=KEY, http_client=http).discover_google_news(query="Topic")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": KEY}))) as http:
        with pytest.raises(CoreClientError) as error:
            await ArgusCoreClient(base_url="https://core.test", api_key=KEY, http_client=http).discover_google_news(query="Topic")
    assert error.value.retry_after is None


@pytest.mark.asyncio
async def test_missing_configuration_never_falls_back(monkeypatch):
    monkeypatch.setattr("app.global_chess_config.global_chess_settings.core_base_url", None)
    with pytest.raises(CoreClientError) as error:
        await discover_google_news_sources("Topic")
    assert error.value.status_code == 503


@pytest.mark.asyncio
async def test_owned_client_disables_proxies_and_closes(envelope):
    http = AsyncMock()
    http.post.return_value = httpx.Response(200, json=envelope)
    with patch("app.clients.argus_core_client.httpx.AsyncClient") as constructor:
        constructor.return_value.__aenter__.return_value = http
        await ArgusCoreClient(base_url="https://core.test", api_key=KEY).discover_google_news(query="Topic")
        constructor.assert_called_once_with(trust_env=False)
        constructor.return_value.__aexit__.assert_awaited_once()
    assert http.post.call_args.kwargs["follow_redirects"] is False
