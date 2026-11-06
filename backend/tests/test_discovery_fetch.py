import gzip
import socket
import zlib
from unittest.mock import AsyncMock

import anyio
import httpcore
import httpx
import pytest

from app.services import article_fetch
from app.services.discovery import fetch
from app.services.discovery.contracts import DiscoveryError

XML = b"<rss><channel/></rss>"


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def response(body=XML, *, status=200, headers=None):
    stream = Body([body])
    result = httpx.Response(status, headers={"content-type": "application/rss+xml", **(headers or {})}, stream=stream)
    result.extensions["test_body"] = stream
    return result


@pytest.fixture(autouse=True)
def forbid_live_sockets(monkeypatch):
    # Production transport still runs; only the final network dial is blocked.
    monkeypatch.setattr(article_fetch.AnyIOBackend, "connect_tcp", AsyncMock(
        side_effect=AssertionError("Unexpected real discovery socket")))
    monkeypatch.setattr(article_fetch.anyio, "getaddrinfo", AsyncMock(
        side_effect=AssertionError("Unexpected real discovery DNS")))


@pytest.mark.asyncio
async def test_fixed_endpoint_query_and_credentials_do_not_escape(monkeypatch, caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    requests = []
    monkeypatch.setenv("HTTPS_PROXY", "http://private-proxy.invalid:8080")
    monkeypatch.setenv("X_API_KEY", "must-not-forward")
    def handler(request):
        requests.append(request)
        assert request.url.host == "news.google.com" and request.url.path == "/rss/search"
        assert dict(request.url.params) == {"q": 'topic & other "quotes"', "hl": "en-US", "gl": "US", "ceid": "US:en"}
        assert not {"x-api-key", "authorization", "proxy-authorization", "cookie"} & set(request.headers)
        return response()
    result = await fetch.SecureDiscoveryFetcher(transport=httpx.MockTransport(handler)).fetch_google_news('topic & other "quotes"')
    assert result == XML and len(requests) == 1
    assert "topic & other" not in caplog.text and "must-not-forward" not in caplog.text
    assert "q=topic" not in caplog.text
    assert requests[0].extensions["timeout"] == {"connect": 5.0, "read": 10.0, "write": 10.0, "pool": 2.0}


@pytest.mark.parametrize("location", [
    "http://127.0.0.1/", "http://169.254.169.254/latest/meta-data/", "https://metadata.internal/",
    "https://public-other.example/", "http://news.google.com/rss", "https://user:password@news.google.com/",
    "https://news.google.com:444/", "file:///etc/passwd", "https://news.google.com/#fragment",
])
@pytest.mark.asyncio
async def test_redirects_fail_closed_before_second_request(location):
    requests = []
    def handler(request):
        requests.append(request)
        return response(status=302, headers={"location": location})
    with pytest.raises(DiscoveryError) as error:
        await fetch.SecureDiscoveryFetcher(transport=httpx.MockTransport(handler)).fetch_google_news("topic")
    assert error.value.status == "unsafe_destination" and len(requests) == 1


@pytest.mark.asyncio
async def test_relative_redirects_and_redirect_budget():
    requests = []
    def handler(request):
        requests.append(request)
        return response(status=302, headers={"location": "/rss/search?q=next"}) if len(requests) == 1 else response()
    await fetch.SecureDiscoveryFetcher(transport=httpx.MockTransport(handler)).fetch_google_news("topic")
    assert len(requests) == 2
    requests.clear()
    def loop(request):
        requests.append(request)
        return response(status=302, headers={"location": "/rss/search"})
    with pytest.raises(DiscoveryError):
        await fetch.SecureDiscoveryFetcher(transport=httpx.MockTransport(loop)).fetch_google_news("topic")
    assert len(requests) == fetch.MAX_REDIRECTS + 1


@pytest.mark.parametrize("status,expected", [(429, "throttled"), (503, "upstream_error"), (401, "upstream_error"), (500, "upstream_error")])
@pytest.mark.asyncio
async def test_provider_errors_no_body_leak_and_no_retry(status, expected):
    requests = []
    body = response(b"private provider diagnostics", status=status, headers={"retry-after": "60"})
    def handler(request):
        requests.append(request)
        return body
    with pytest.raises(DiscoveryError) as error:
        await fetch.SecureDiscoveryFetcher(transport=httpx.MockTransport(handler)).fetch_google_news("sensitive query")
    assert error.value.status == expected and error.value.retry_after == "60"
    assert "private" not in str(error.value) and "sensitive" not in str(error.value)
    assert body.extensions["test_body"].closed and len(requests) == 1


@pytest.mark.parametrize("kind", ["network", "timeout", "incomplete"])
@pytest.mark.asyncio
async def test_transport_errors_are_sanitized(kind):
    def handler(request):
        cls = {"network": httpx.ConnectError, "timeout": httpx.ReadTimeout, "incomplete": httpx.RemoteProtocolError}[kind]
        raise cls("secret request URL and diagnostics", request=request)
    with pytest.raises(DiscoveryError) as error:
        await fetch.SecureDiscoveryFetcher(transport=httpx.MockTransport(handler)).fetch_google_news("topic")
    assert error.value.status == ("timeout" if kind == "timeout" else "upstream_error")
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
@pytest.mark.asyncio
async def test_bounded_compression_is_supported(encoding):
    body = XML if encoding == "identity" else gzip.compress(XML) if encoding == "gzip" else zlib.compress(XML)
    r = response(body, headers={"content-encoding": encoding, "content-length": str(len(body))})
    assert await fetch.SecureDiscoveryFetcher(transport=httpx.MockTransport(lambda rqt: r)).fetch_google_news("topic") == XML
    assert r.extensions["test_body"].closed


@pytest.mark.parametrize("fault", ["declared", "streamed", "gzip_bomb", "deflate_bomb", "incomplete_gzip", "bad_deflate", "trailing_gzip", "encoding", "length", "short", "content_type"])
@pytest.mark.asyncio
async def test_response_limits_and_integrity(fault, monkeypatch):
    monkeypatch.setattr(fetch, "MAX_DOWNLOAD_BYTES", 128)
    monkeypatch.setattr(fetch, "MAX_XML_BYTES", 128)
    headers = {}
    body = XML
    expected = "response_too_large"
    if fault == "declared": headers["content-length"] = "129"
    elif fault == "streamed": body = b"x" * 129
    elif fault in {"gzip_bomb", "deflate_bomb"}:
        headers["content-encoding"] = "gzip" if fault == "gzip_bomb" else "deflate"
        body = gzip.compress(b"x" * 10000) if fault == "gzip_bomb" else zlib.compress(b"x" * 10000)
    elif fault == "incomplete_gzip":
        headers["content-encoding"] = "gzip"; body = gzip.compress(XML)[:-3]; expected = "malformed_feed"
    elif fault == "bad_deflate":
        headers["content-encoding"] = "deflate"; body = b"invalid"; expected = "malformed_feed"
    elif fault == "trailing_gzip":
        headers["content-encoding"] = "gzip"; body = gzip.compress(XML) + gzip.compress(XML); expected = "malformed_feed"
    elif fault == "encoding": headers["content-encoding"] = "br"; expected = "unsupported_encoding"
    elif fault == "length": headers["content-length"] = "invalid"; expected = "malformed_feed"
    elif fault == "short": headers["content-length"] = "100"; expected = "malformed_feed"
    elif fault == "content_type": headers["content-type"] = "text/html"; expected = "malformed_feed"
    r = response(body, headers=headers)
    with pytest.raises(DiscoveryError) as error:
        await fetch.SecureDiscoveryFetcher(transport=httpx.MockTransport(lambda rqt: r)).fetch_google_news("topic")
    assert error.value.status == expected and r.extensions["test_body"].closed


@pytest.mark.asyncio
async def test_total_timeout_closes_trickling_stream(monkeypatch):
    class Trickle(Body):
        async def __aiter__(self):
            while True:
                await anyio.sleep(0.01)
                yield b" "
    stream = Trickle([])
    r = httpx.Response(200, headers={"content-type": "text/xml"}, stream=stream)
    r.extensions["test_body"] = stream
    monkeypatch.setattr(fetch, "FETCH_DEADLINE", 0.03)
    with pytest.raises(DiscoveryError) as error:
        await fetch.SecureDiscoveryFetcher(transport=httpx.MockTransport(lambda rqt: r)).fetch_google_news("topic")
    assert error.value.status == "timeout" and r.extensions["test_body"].closed


def records(*addresses):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]


@pytest.mark.parametrize("addresses", [("127.0.0.1",), ("169.254.169.254",), ("93.184.216.34", "10.0.0.1")])
@pytest.mark.asyncio
async def test_default_transport_rejects_unsafe_dns_before_dial(addresses, monkeypatch):
    monkeypatch.setattr(article_fetch.anyio, "getaddrinfo", AsyncMock(return_value=records(*addresses)))
    dial = AsyncMock()
    monkeypatch.setattr(article_fetch.AnyIOBackend, "connect_tcp", dial)
    with pytest.raises(DiscoveryError) as error:
        await fetch.SecureDiscoveryFetcher().fetch_google_news("topic")
    assert error.value.status == "unsafe_destination"
    dial.assert_not_called()


@pytest.mark.asyncio
async def test_default_transport_pins_ip_preserves_tls_and_disables_proxies(monkeypatch):
    class Wire(httpcore.AsyncNetworkStream):
        def __init__(self):
            self.data = b"HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\nContent-Length: 21\r\n\r\n" + XML
            self.writes = []
            self.sni = None
        async def read(self, max_bytes, timeout=None):
            assert timeout == 10.0
            chunk, self.data = self.data[:max_bytes], self.data[max_bytes:]
            return chunk
        async def write(self, buffer, timeout=None):
            assert timeout == 10.0
            self.writes.append(buffer)
        async def aclose(self): pass
        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            assert ssl_context.check_hostname
            assert timeout == 5.0
            self.sni = server_hostname
            return self
    wire = Wire()
    # A rebinding DNS service would return private IPs on a second resolution.
    resolver = AsyncMock(side_effect=[records("93.184.216.34"), records("127.0.0.1")])
    monkeypatch.setattr(article_fetch.anyio, "getaddrinfo", resolver)
    dial = AsyncMock(return_value=wire)
    monkeypatch.setattr(article_fetch.AnyIOBackend, "connect_tcp", dial)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1234")
    assert await fetch.SecureDiscoveryFetcher().fetch_google_news("topic") == XML
    assert resolver.await_count == 1 and dial.call_args.args[:2] == ("93.184.216.34", 443)
    assert dial.call_args.kwargs["timeout"] == 5.0
    assert wire.sni == "news.google.com" and b"Host: news.google.com" in b"".join(wire.writes)


@pytest.mark.parametrize("value,expected", [(None, None), ("invalid", None), ("60", "60"), ("9999999999", "86400"), ("-1", None), ("1\r\nsecret", None)])
def test_retry_after_sanitization(value, expected):
    assert fetch.retry_after_seconds(value) == expected
