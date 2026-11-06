"""Security regressions; all DNS and sockets are fakes, never real services."""

import socket
import gzip
import zlib
import ssl
from importlib.metadata import version
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest
import anyio
import anyio._core._sockets as anyio_sockets

from app.services import article_extraction as extraction
from app.services import article_fetch as fetch


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("url", [
    "http://localhost/a", "http://sub.localhost/a", "http://LOCALHOST./",
    "http://127.0.0.1/", "http://127.1/", "http://2130706433/",
    "http://10.0.0.1/", "http://172.16.0.1/", "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/", "http://169.254.170.2/",
    "http://metadata.google.internal/", "http://168.63.129.16/",
    "http://0.0.0.0/", "http://100.100.100.200/", "http://224.0.0.1/",
    "http://[::1]/", "http://[fe80::1]/", "http://[fc00::1]/",
    "http://[fec0::1]/", "http://198.18.0.1/", "http://192.0.2.1/",
    "http://[::ffff:127.0.0.1]/", "http://[64:ff9b::7f00:1]/",
    "http://[fe80::1%25eth0]/", "file:///etc/passwd", "ftp://example.com/",
    "http://user:password@example.com/",
])
@pytest.mark.asyncio
async def test_blocked_destinations_never_connect(url, monkeypatch):
    # Odd numeric spellings are DNS-resolved by the OS; emulate its answer.
    dns = AsyncMock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))])
    dial = AsyncMock()
    monkeypatch.setattr(fetch.anyio, "getaddrinfo", dns)
    monkeypatch.setattr(fetch.AnyIOBackend, "connect_tcp", dial)
    async with httpx.AsyncClient(transport=fetch.PublicHTTPTransport(), trust_env=False) as client:
        result = await extraction.extract_article(url, client)
    assert result["extraction_status"] in {"failed", "request_error"}
    assert result["extraction_error"] in {"Invalid URL", "Unsafe article destination."}
    assert result["article_text"] is None
    dial.assert_not_called()


def records(*addresses):
    return [(socket.AF_INET6 if ":" in address else socket.AF_INET,
             socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]


@pytest.mark.parametrize("addresses", [
    ("127.0.0.1",), ("10.1.2.3",), ("169.254.169.254",),
    ("93.184.216.34", "192.168.0.1"), ("::1",), ("fe80::1",), (),
])
@pytest.mark.asyncio
async def test_dns_answers_fail_closed(addresses, monkeypatch):
    monkeypatch.setattr(fetch.anyio, "getaddrinfo", AsyncMock(return_value=records(*addresses)))
    dial = AsyncMock()
    monkeypatch.setattr(fetch.AnyIOBackend, "connect_tcp", dial)
    with pytest.raises((fetch.UnsafeDestination, httpcore.ConnectError)):
        await fetch.PublicNetworkBackend().connect_tcp("news.example", 443, timeout=1)
    dial.assert_not_called()


@pytest.mark.asyncio
async def test_rebinding_connects_only_to_validated_ip(monkeypatch):
    dns = AsyncMock(side_effect=[records("93.184.216.34"), records("127.0.0.1")])
    stream = object()
    dial = AsyncMock(return_value=stream)
    monkeypatch.setattr(fetch.anyio, "getaddrinfo", dns)
    monkeypatch.setattr(fetch.AnyIOBackend, "connect_tcp", dial)
    backend = fetch.PublicNetworkBackend()
    assert await backend.connect_tcp("news.example", 443, timeout=1) is stream
    assert dial.call_args.args == ("93.184.216.34", 443)
    dns.assert_awaited_once()
    with pytest.raises(fetch.UnsafeDestination):
        await backend.connect_tcp("news.example", 443, timeout=1)
    assert dial.await_count == 1


@pytest.mark.asyncio
async def test_public_ipv6_and_fallback(monkeypatch):
    monkeypatch.setattr(fetch.anyio, "getaddrinfo", AsyncMock(
        return_value=records("93.184.216.34", "2606:4700:4700::1111")))
    dial = AsyncMock(side_effect=[httpcore.ConnectError("unavailable"), object()])
    monkeypatch.setattr(fetch.AnyIOBackend, "connect_tcp", dial)
    await fetch.PublicNetworkBackend().connect_tcp("news.example", 443)
    assert [call.args[0] for call in dial.await_args_list] == [
        "93.184.216.34", "2606:4700:4700::1111"]


@pytest.mark.parametrize("error,expected", [
    (socket.gaierror("not found"), httpcore.ConnectError),
    (TimeoutError(), httpcore.ConnectTimeout),
])
@pytest.mark.asyncio
async def test_dns_failure_never_connects(error, expected, monkeypatch):
    monkeypatch.setattr(fetch.anyio, "getaddrinfo", AsyncMock(side_effect=error))
    dial = AsyncMock()
    monkeypatch.setattr(fetch.AnyIOBackend, "connect_tcp", dial)
    with pytest.raises(expected):
        await fetch.PublicNetworkBackend().connect_tcp("news.example", 443, timeout=1)
    dial.assert_not_called()


@pytest.mark.parametrize("location", ["http://127.0.0.1/", "//169.254.169.254/", "file:///etc/passwd"])
@pytest.mark.asyncio
async def test_unsafe_redirect_is_not_requested(location):
    requests = []
    body = Body([b"redirect body must not be downloaded"])
    def respond(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": location}, stream=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await extraction.extract_article("https://news.example/article", client)
    assert result["extraction_status"] == "request_error"
    assert len(requests) == 1
    assert body.reads == 0 and body.closed


@pytest.mark.asyncio
async def test_every_redirect_destination_is_checked(monkeypatch):
    # Exercise the real HTTPX/httpcore transport with fake TCP streams.
    dns = AsyncMock(side_effect=[records("93.184.216.34"), records("10.0.0.1")])
    monkeypatch.setattr(fetch.anyio, "getaddrinfo", dns)
    wire = WireStream(b"HTTP/1.1 302 Found\r\nLocation: http://evil.example/\r\nContent-Length: 0\r\n\r\n")
    dial = AsyncMock(return_value=wire)
    monkeypatch.setattr(fetch.AnyIOBackend, "connect_tcp", dial)
    async with httpx.AsyncClient(transport=fetch.PublicHTTPTransport(), trust_env=False) as client:
        result = await extraction.extract_article("http://news.example/", client)
    assert result["extraction_status"] == "request_error"
    assert dns.await_count == 2
    assert dial.await_count == 1


@pytest.mark.asyncio
async def test_public_relative_and_cross_origin_redirects(monkeypatch):
    urls = []
    def respond(request):
        urls.append(str(request.url))
        if len(urls) < 3:
            return httpx.Response(302, headers={"location": "/next" if len(urls) == 1 else "http://other.example/final"})
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=Body([b"<html>article</html>"]))
    monkeypatch.setattr(extraction.trafilatura, "extract", lambda *a, **kw: "Article text. " * 30)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await extraction.extract_article("https://news.example/start", client)
    assert result["extraction_status"] == "success"
    assert urls == ["https://news.example/start", "https://news.example/next", "http://other.example/final"]


@pytest.mark.asyncio
async def test_redirect_loop_is_bounded():
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "/loop"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await extraction.extract_article("https://news.example/", client)
    assert result["extraction_status"] == "request_error"
    assert len(calls) == extraction.MAX_REDIRECTS + 1


@pytest.mark.parametrize("headers,chunks,reads", [
    ({"content-length": "33"}, [b"x"], 0),
    ({}, [b"x" * 20, b"x" * 20, b"not read"], 2),
    ({"content-length": "1"}, [b"x" * 33], 1),
    ({"content-encoding": "br"}, [b"unsupported encoding"], 0),
    ({"content-encoding": "gzip"}, [gzip.compress(b"x" * 10000)], 1),
])
@pytest.mark.asyncio
async def test_download_limit_closes_stream(headers, chunks, reads, monkeypatch):
    monkeypatch.setattr(extraction, "MAX_RESPONSE_BYTES", 32)
    parser = AsyncMock()
    monkeypatch.setattr(extraction.trafilatura, "extract", parser)
    body = Body(chunks)
    def respond(request):
        return httpx.Response(200, headers={"content-type": "text/html", **headers}, stream=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await extraction.extract_article("https://news.example/", client)
    assert result["extraction_status"] == "request_error"
    assert body.reads == reads and body.closed
    parser.assert_not_called()


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
@pytest.mark.asyncio
async def test_bounded_public_article_and_compression(encoding, monkeypatch):
    html = b"<html>article</html>"
    compressed = {"identity": html, "gzip": gzip.compress(html), "deflate": zlib.compress(html)}[encoding]
    body = Body([compressed])
    monkeypatch.setattr(extraction, "MAX_RESPONSE_BYTES", max(len(compressed), len(html)))
    seen = []
    def parse(text, **kwargs):
        seen.append(text)
        return "Article body. " * 30
    monkeypatch.setattr(extraction.trafilatura, "extract", parse)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/html", "content-encoding": encoding}, stream=body,
    ))) as client:
        result = await extraction.extract_article("https://news.example/", client)
    assert result["extraction_status"] == "success"
    assert seen == [html.decode()]
    assert body.closed


@pytest.mark.asyncio
async def test_compressed_expansion_limit(monkeypatch):
    monkeypatch.setattr(extraction, "MAX_RESPONSE_BYTES", 256)
    body = Body([gzip.compress(b"x" * 10000)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/html", "content-encoding": "gzip"}, stream=body,
    ))) as client:
        result = await extraction.extract_article("https://news.example/", client)
    assert result["extraction_status"] == "request_error"
    assert "download limit" in result["extraction_error"]
    assert body.closed


class WireStream(httpcore.AsyncNetworkStream):
    def __init__(self, response):
        self.response = response
        self.writes = []
        self.server_hostname = None

    async def read(self, max_bytes, timeout=None):
        data, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return data

    async def write(self, buffer, timeout=None):
        self.writes.append(buffer)

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.server_hostname = server_hostname
        assert ssl_context.check_hostname
        assert ssl_context.verify_mode == ssl.CERT_REQUIRED
        return self

    async def aclose(self):
        pass


@pytest.mark.parametrize("scheme", ["http", "https"])
@pytest.mark.asyncio
async def test_real_transport_keeps_host_and_tls_identity(scheme, monkeypatch):
    monkeypatch.setattr(fetch.anyio, "getaddrinfo", AsyncMock(return_value=records("93.184.216.34")))
    wire = WireStream(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 13\r\n\r\n<html></html>")
    dial = AsyncMock(return_value=wire)
    monkeypatch.setattr(fetch.AnyIOBackend, "connect_tcp", dial)
    monkeypatch.setattr(extraction.trafilatura, "extract", lambda *a, **kw: "Article text. " * 30)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1234")
    async with httpx.AsyncClient(transport=fetch.PublicHTTPTransport(), trust_env=False) as client:
        result = await extraction.extract_article(f"{scheme}://news.example/article", client)
    assert result["extraction_status"] == "success"
    assert dial.call_args.args[0] == "93.184.216.34"
    assert b"Host: news.example" in b"".join(wire.writes)
    if scheme == "https":
        assert wire.server_hostname == "news.example"


def test_supported_dependency_versions():
    assert {name: version(name) for name in ("httpx", "httpcore", "anyio")} == {
        "httpx": "0.28.1", "httpcore": "1.0.9", "anyio": "4.15.1",
    }


@pytest.mark.asyncio
async def test_actual_anyio_numeric_dial_does_not_resolve_again(monkeypatch):
    # Keep HTTPX, httpcore and AnyIO connect_tcp real. Fake only DNS and the
    # async platform backend's final numeric socket operation (no real network).
    class Socket:
        def __init__(self):
            self.data = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"

        async def send(self, item):
            pass

        async def receive(self, max_bytes):
            data, self.data = self.data[:max_bytes], self.data[max_bytes:]
            return data

        def extra(self, attribute, default=None):
            return default

        async def aclose(self):
            pass

    dns = AsyncMock(side_effect=[records("93.184.216.34"), records("127.0.0.1")])
    second_lookup = AsyncMock(return_value=records("127.0.0.1"))
    dial = AsyncMock(return_value=Socket())
    from types import SimpleNamespace
    monkeypatch.setattr(fetch.anyio, "getaddrinfo", dns)
    monkeypatch.setattr(anyio_sockets, "getaddrinfo", second_lookup)
    monkeypatch.setattr(anyio_sockets, "get_async_backend", lambda: SimpleNamespace(connect_tcp=dial))
    async with httpx.AsyncClient(transport=fetch.PublicHTTPTransport(), trust_env=False) as client:
        response = await client.get("http://news.example/")
    assert response.text == "ok"
    dns.assert_awaited_once()
    second_lookup.assert_not_called()
    assert dial.call_args.args[:2] == ("93.184.216.34", 80)


class KeepAliveStream(WireStream):
    def __init__(self, close=False):
        super().__init__(b"")
        self.close = close

    async def write(self, buffer, timeout=None):
        await super().write(buffer, timeout)
        if buffer.startswith(b"GET "):
            self.response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
            if self.close:
                self.response += b"Connection: close\r\n"
            self.response += b"\r\nok"


@pytest.mark.asyncio
async def test_pool_reuse_stays_on_pinned_peer_and_reconnect_revalidates(monkeypatch):
    dns = AsyncMock(side_effect=[records("93.184.216.34"), records("127.0.0.1")])
    wire = KeepAliveStream()
    dial = AsyncMock(return_value=wire)
    monkeypatch.setattr(fetch.anyio, "getaddrinfo", dns)
    monkeypatch.setattr(fetch.AnyIOBackend, "connect_tcp", dial)
    transport = fetch.PublicHTTPTransport()
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        assert (await client.get("http://news.example/one")).text == "ok"
        assert (await client.get("http://news.example/two")).text == "ok"
        dns.assert_awaited_once()
        dial.assert_awaited_once()
        # Close the peer so the next request must establish a new connection.
        await transport._pool.aclose()
        with pytest.raises(fetch.UnsafeDestination):
            await client.get("http://news.example/three")
    assert dns.await_count == 2
    assert dial.await_count == 1
    assert dial.call_args.args[0] == "93.184.216.34"


@pytest.mark.parametrize("other", ["http://other.example/", "http://news.example:81/", "https://news.example/"])
@pytest.mark.asyncio
async def test_pool_never_reuses_different_origin(other, monkeypatch):
    dns = AsyncMock(side_effect=[records("93.184.216.34"), records("10.0.0.1")])
    dial = AsyncMock(return_value=KeepAliveStream())
    monkeypatch.setattr(fetch.anyio, "getaddrinfo", dns)
    monkeypatch.setattr(fetch.AnyIOBackend, "connect_tcp", dial)
    async with httpx.AsyncClient(transport=fetch.PublicHTTPTransport(), trust_env=False) as client:
        await client.get("http://news.example/")
        with pytest.raises(fetch.UnsafeDestination):
            await client.get(other)
    assert dns.await_count == 2
    assert dial.await_count == 1


@pytest.mark.asyncio
async def test_sni_override_cannot_change_certificate_identity(monkeypatch):
    wire = KeepAliveStream()
    monkeypatch.setattr(fetch.anyio, "getaddrinfo", AsyncMock(return_value=records("93.184.216.34")))
    monkeypatch.setattr(fetch.AnyIOBackend, "connect_tcp", AsyncMock(return_value=wire))
    async with httpx.AsyncClient(transport=fetch.PublicHTTPTransport(), trust_env=False) as client:
        await client.get("https://news.example/", extensions={"sni_hostname": "attacker.example"})
    assert wire.server_hostname == "news.example"


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
@pytest.mark.parametrize("fault", ["garbage", "truncated", "checksum", "trailing", "concatenated", "oversized", "exact_limit"])
@pytest.mark.asyncio
async def test_compressed_stream_integrity_and_limits(encoding, fault, monkeypatch):
    limit = 256
    monkeypatch.setattr(extraction, "MAX_RESPONSE_BYTES", limit)
    raw = b"a" * (limit + 1 if fault == "oversized" else limit)
    encoded = gzip.compress(raw) if encoding == "gzip" else zlib.compress(raw)
    if fault == "garbage":
        encoded = b"invalid compressed data"
    elif fault == "truncated":
        encoded = encoded[:-1]
    elif fault == "checksum":
        encoded = encoded[:-1] + bytes([encoded[-1] ^ 255])
    elif fault == "trailing":
        encoded += b"trailing bytes"
    elif fault == "concatenated":
        encoded += encoded
    body = Body([encoded[:5], encoded[5:]])
    parser = AsyncMock()
    monkeypatch.setattr(extraction.trafilatura, "extract", parser)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/html", "content-encoding": encoding}, stream=body,
    ))) as client:
        if fault == "exact_limit":
            response = await extraction.download_article("https://news.example/", client)
            assert response.content == raw
        else:
            result = await extraction.extract_article("https://news.example/", client)
            assert result["extraction_status"] == "request_error"
            assert result["article_text"] is None
    assert body.closed
    parser.assert_not_called()


@pytest.mark.asyncio
async def test_incomplete_http_body_maps_to_request_error(monkeypatch):
    wire = WireStream(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 100\r\n\r\nshort")
    monkeypatch.setattr(fetch.anyio, "getaddrinfo", AsyncMock(return_value=records("93.184.216.34")))
    monkeypatch.setattr(fetch.AnyIOBackend, "connect_tcp", AsyncMock(return_value=wire))
    async with httpx.AsyncClient(transport=fetch.PublicHTTPTransport(), trust_env=False) as client:
        result = await extraction.extract_article("http://news.example/", client)
    assert result["extraction_status"] == "request_error"
    assert result["article_text"] is None


@pytest.mark.parametrize("phase", ["dns", "tcp", "tls", "read", "write"])
@pytest.mark.asyncio
async def test_transport_timeout_mapping(phase, monkeypatch):
    async def stall(*args, **kwargs):
        await anyio.sleep_forever()
    wire = KeepAliveStream()
    dns = AsyncMock(return_value=records("93.184.216.34"))
    dial = AsyncMock(return_value=wire)
    if phase == "dns":
        dns.side_effect = stall
    elif phase == "tcp":
        dial.side_effect = stall
    elif phase == "tls":
        wire.start_tls = AsyncMock(side_effect=httpcore.ConnectTimeout("TLS timeout"))
    elif phase == "read":
        wire.read = AsyncMock(side_effect=httpcore.ReadTimeout("Read timeout"))
    else:
        wire.write = AsyncMock(side_effect=httpcore.WriteTimeout("Write timeout"))
    monkeypatch.setattr(fetch.anyio, "getaddrinfo", dns)
    monkeypatch.setattr(fetch.AnyIOBackend, "connect_tcp", dial)
    async with httpx.AsyncClient(transport=fetch.PublicHTTPTransport(), trust_env=False, timeout=0.02) as client:
        result = await extraction.extract_article("https://news.example/", client)
    assert result["extraction_status"] == "timeout"


@pytest.mark.asyncio
async def test_total_deadline_stops_trickle_response(monkeypatch):
    class Trickle(Body):
        async def __aiter__(self):
            while True:
                self.reads += 1
                yield b"a"
                await anyio.sleep(0.005)
    monkeypatch.setattr(extraction, "REQUEST_TIMEOUT", 0.03)
    body = Trickle([])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "text/html"}, stream=body,
    ))) as client:
        result = await extraction.extract_article("https://news.example/", client)
    assert result["extraction_status"] == "timeout"
    assert body.closed and body.reads > 0


@pytest.mark.asyncio
async def test_pool_timeout_is_preserved(monkeypatch):
    monkeypatch.setattr(fetch.anyio, "getaddrinfo", AsyncMock(return_value=records("93.184.216.34")))
    monkeypatch.setattr(fetch.AnyIOBackend, "connect_tcp", AsyncMock(return_value=KeepAliveStream()))
    transport = fetch.PublicHTTPTransport()
    transport._pool._max_connections = 1
    async with httpx.AsyncClient(transport=transport, trust_env=False, timeout=0.02) as client:
        # An unread streamed response occupies the only pool slot.
        response = await client.send(client.build_request("GET", "http://news.example/"), stream=True)
        try:
            with pytest.raises(httpx.PoolTimeout):
                await client.get("http://other.example/")
        finally:
            await response.aclose()


@pytest.mark.parametrize("phase", ["tls", "read", "write"])
@pytest.mark.asyncio
async def test_real_anyio_stream_enforces_phase_deadline(phase, monkeypatch):
    from httpcore._backends.anyio import AnyIOStream

    async def stall(*args, **kwargs):
        await anyio.sleep_forever()

    raw = type("RawStream", (), {})()
    raw.send = AsyncMock(side_effect=stall if phase == "write" else None)
    raw.receive = AsyncMock(side_effect=stall)
    raw.aclose = AsyncMock()
    raw.extra = lambda attribute, default=None: default
    monkeypatch.setattr(fetch.anyio, "getaddrinfo", AsyncMock(return_value=records("93.184.216.34")))
    monkeypatch.setattr(fetch.AnyIOBackend, "connect_tcp", AsyncMock(return_value=AnyIOStream(raw)))
    if phase == "tls":
        monkeypatch.setattr(anyio.streams.tls.TLSStream, "wrap", AsyncMock(side_effect=stall))
    scheme = "https" if phase == "tls" else "http"
    expected = {"tls": httpx.ConnectTimeout, "read": httpx.ReadTimeout, "write": httpx.WriteTimeout}[phase]
    async with httpx.AsyncClient(transport=fetch.PublicHTTPTransport(), trust_env=False, timeout=0.02) as client:
        with pytest.raises(expected):
            await client.get(f"{scheme}://news.example/")


@pytest.mark.asyncio
async def test_certificate_verification_failure_fails_closed(monkeypatch):
    from httpcore._backends.anyio import AnyIOStream

    raw = type("RawStream", (), {})()
    raw.aclose = AsyncMock()
    raw.extra = lambda attribute, default=None: default
    monkeypatch.setattr(fetch.anyio, "getaddrinfo", AsyncMock(return_value=records("93.184.216.34")))
    monkeypatch.setattr(fetch.AnyIOBackend, "connect_tcp", AsyncMock(return_value=AnyIOStream(raw)))
    async def verify(stream, **kwargs):
        assert kwargs["hostname"] == "news.example"
        assert kwargs["ssl_context"].check_hostname
        assert kwargs["ssl_context"].verify_mode == ssl.CERT_REQUIRED
        raise ssl.SSLCertVerificationError("hostname mismatch")
    monkeypatch.setattr(anyio.streams.tls.TLSStream, "wrap", verify)
    async with httpx.AsyncClient(transport=fetch.PublicHTTPTransport(), trust_env=False) as client:
        with pytest.raises(httpx.ConnectError, match="hostname mismatch"):
            await client.get("https://news.example/")
    raw.aclose.assert_awaited_once()
