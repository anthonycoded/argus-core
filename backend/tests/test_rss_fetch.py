import gzip
import socket
from unittest.mock import AsyncMock

import anyio
import httpcore
import httpx
import pytest

from app.services import article_fetch
from app.services.discovery import fetch, rss_fetch
from app.services.discovery.contracts import DiscoveryError
from app.services.discovery.feed_registry import APPROVED_FEEDS
from app.services.discovery.rss_fetch import SecureRSSFetcher

XML = b"<rss><channel/></rss>"


class Body(httpx.AsyncByteStream):
    def __init__(self, content):self.content=content;self.closed=False
    async def __aiter__(self):yield self.content
    async def aclose(self):self.closed=True


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(article_fetch.anyio,"getaddrinfo",AsyncMock(side_effect=AssertionError("Live DNS")))
    monkeypatch.setattr(article_fetch.AnyIOBackend,"connect_tcp",AsyncMock(side_effect=AssertionError("Live socket")))


@pytest.mark.parametrize("feed_id", APPROVED_FEEDS)
@pytest.mark.asyncio
async def test_fixed_feed_requests_no_credentials_or_proxy(feed_id,monkeypatch,caplog):
    import logging
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("HTTPS_PROXY","http://private-proxy.invalid")
    calls=[]
    def respond(request):
        calls.append(request)
        assert str(request.url)==APPROVED_FEEDS[feed_id].url
        assert not {"authorization","x-api-key","cookie","proxy-authorization"} & set(request.headers)
        return httpx.Response(200,headers={"content-type":"application/rss+xml"},stream=Body(XML))
    result=await SecureRSSFetcher(httpx.MockTransport(respond)).fetch(APPROVED_FEEDS[feed_id])
    assert result.data==XML and len(calls)==1
    assert APPROVED_FEEDS[feed_id].url not in caplog.text


@pytest.mark.parametrize("location",["http://127.0.0.1/","https://169.254.169.254/","https://metadata.internal/",
    "https://evil.example/rss","https://feeds.npr.org/1014/rss.xml","http://feeds.bbci.co.uk/rss",
    "https://user:secret@feeds.bbci.co.uk/rss","file:///etc/passwd"])
@pytest.mark.asyncio
async def test_unapproved_redirect_never_requested(location):
    calls=[]
    def respond(request):
        calls.append(request);return httpx.Response(302,headers={"location":location},stream=Body(b""))
    with pytest.raises(DiscoveryError) as error:
        await SecureRSSFetcher(httpx.MockTransport(respond)).fetch(APPROVED_FEEDS["bbc-world"])
    assert error.value.status=="unsafe_destination" and len(calls)==1


@pytest.mark.asyncio
async def test_relative_redirect_and_loop_budget():
    calls=[]
    def respond(request):
        calls.append(request)
        if len(calls)==1:return httpx.Response(302,headers={"location":"/news/world/next.xml"},stream=Body(b""))
        return httpx.Response(200,headers={"content-type":"text/xml"},stream=Body(XML))
    assert (await SecureRSSFetcher(httpx.MockTransport(respond)).fetch(APPROVED_FEEDS["bbc-world"])).data==XML
    assert len(calls)==2
    calls.clear()
    def loop(request):
        calls.append(request);return httpx.Response(302,headers={"location":"/rss"},stream=Body(b""))
    with pytest.raises(DiscoveryError):await SecureRSSFetcher(httpx.MockTransport(loop)).fetch(APPROVED_FEEDS["bbc-world"])
    assert len(calls)==4


@pytest.mark.parametrize("kind,status",[("declared","response_too_large"),("stream","response_too_large"),
    ("gzip","response_too_large"),("truncated","malformed_feed"),("429","throttled"),("503","upstream_error")])
@pytest.mark.asyncio
async def test_response_limits_errors_and_cleanup(kind,status,monkeypatch):
    monkeypatch.setattr(fetch,"MAX_DOWNLOAD_BYTES",128);monkeypatch.setattr(fetch,"MAX_XML_BYTES",128)
    headers={"content-type":"text/xml"};data=XML;code=200
    if kind=="declared":headers["content-length"]="129"
    elif kind=="stream":data=b"x"*129
    elif kind=="gzip":headers["content-encoding"]="gzip";data=gzip.compress(b"x"*10000)
    elif kind=="truncated":headers["content-encoding"]="gzip";data=gzip.compress(XML)[:-2]
    else:code=int(kind);headers["retry-after"]="120"
    body=Body(data)
    with pytest.raises(DiscoveryError) as error:
        await SecureRSSFetcher(httpx.MockTransport(lambda r:httpx.Response(code,headers=headers,stream=body))).fetch(APPROVED_FEEDS["bbc-world"])
    assert error.value.status==status and body.closed
    if kind=="429":assert error.value.retry_after=="120"


@pytest.mark.asyncio
async def test_timeout_closes_trickle(monkeypatch):
    class Trickle(Body):
        async def __aiter__(self):
            while True:
                await anyio.sleep(0.01);yield b" "
    monkeypatch.setattr(rss_fetch,"FETCH_DEADLINE",0.03)
    body=Trickle(b"")
    with pytest.raises(DiscoveryError) as error:
        await SecureRSSFetcher(httpx.MockTransport(lambda r:httpx.Response(200,headers={"content-type":"text/xml"},stream=body))).fetch(APPROVED_FEEDS["bbc-world"])
    assert error.value.status=="timeout" and body.closed


@pytest.mark.parametrize("addresses",[("127.0.0.1",),("169.254.169.254",),("93.184.216.34","10.0.0.1")])
@pytest.mark.asyncio
async def test_unsafe_resolved_addresses_never_dial(addresses,monkeypatch):
    records=[(socket.AF_INET,socket.SOCK_STREAM,6,"",(ip,443)) for ip in addresses]
    monkeypatch.setattr(article_fetch.anyio,"getaddrinfo",AsyncMock(return_value=records))
    dial=AsyncMock();monkeypatch.setattr(article_fetch.AnyIOBackend,"connect_tcp",dial)
    with pytest.raises(DiscoveryError) as error:await SecureRSSFetcher().fetch(APPROVED_FEEDS["bbc-world"])
    assert error.value.status=="unsafe_destination";dial.assert_not_called()


@pytest.mark.asyncio
async def test_pinned_numerical_connection_and_tls_identity(monkeypatch):
    class Wire(httpcore.AsyncNetworkStream):
        def __init__(self):self.data=b"HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\nContent-Length: 21\r\n\r\n"+XML;self.sni=None;self.writes=[]
        async def read(self,max_bytes,timeout=None):chunk,self.data=self.data[:max_bytes],self.data[max_bytes:];return chunk
        async def write(self,buffer,timeout=None):self.writes.append(buffer)
        async def aclose(self):pass
        async def start_tls(self,ssl_context,server_hostname=None,timeout=None):
            assert ssl_context.check_hostname;self.sni=server_hostname;return self
    wire=Wire()
    def records(ip):return [(socket.AF_INET,socket.SOCK_STREAM,6,"",(ip,443))]
    dns=AsyncMock(side_effect=[records("93.184.216.34"),records("127.0.0.1")])
    monkeypatch.setattr(article_fetch.anyio,"getaddrinfo",dns)
    dial=AsyncMock(return_value=wire);monkeypatch.setattr(article_fetch.AnyIOBackend,"connect_tcp",dial)
    assert (await SecureRSSFetcher().fetch(APPROVED_FEEDS["bbc-world"])).data==XML
    assert dns.await_count==1 and dial.call_args.args[0]=="93.184.216.34"
    assert wire.sni=="feeds.bbci.co.uk" and b"Host: feeds.bbci.co.uk" in b"".join(wire.writes)
