"""Synthetic JSON and mocked transports only; never issue a live GDELT query."""

import asyncio
import gzip
import json
import socket
import ssl
import zlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import httpcore
import httpx
import pytest
from pydantic import ValidationError

from app.services.discovery import gdelt, gdelt_fetch
from app.services.discovery.gdelt import GDELTProvider, parse_gdelt
from app.services.discovery.gdelt_contracts import GDELTFault, GDELTRequest, RetryPolicy
from app.services.discovery.gdelt_fetch import SecureGDELTFetcher, retry_policy

POOL_HANDLER = httpcore.AsyncConnectionPool.handle_async_request


def request(**extra):return GDELTRequest(provider="gdelt",query='Ukraine "foreign policy"',**extra)


def article(**extra):
    return {"title":"Supplied report","url":"https://example.com/report?utm_source=gdelt", "domain":"reported.example",
            "seendate":"20261106T120000Z","language":"English","sourcecountry":"United States",
            "socialimage":"https://example.com/image.jpg","url_mobile":"https://m.example.com/report",**extra}


def document(*rows):return json.dumps({"articles":list(rows)}).encode()


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(httpcore.AsyncConnectionPool,"handle_async_request",AsyncMock(side_effect=AssertionError("Live HTTP")))


@pytest.mark.parametrize("query",['', ' ', 'Ukraine OR NATO', '(Ukraine)', 'domain:example.com', 'Ukraine*', '-Russia',
    '"unterminated', 'Ukraine"NATO"', '""', 'a\nb', 'x'*1001, ' '.join(['word']*17), '"'+('word '*33)+'"'])
def test_query_complexity_is_rejected(query):
    with pytest.raises(ValidationError):GDELTRequest(provider="gdelt",query=query)


@pytest.mark.parametrize("extra",[{"limit":0},{"limit":76},{"limit":True},{"limit":"5"},{"window_hours":0},
    {"window_hours":169},{"window_hours":True},{"url":"http://localhost"},{"headers":{}},{"endpoint":"https://evil.example"},
    {"cursor":"cursor"},{"enabled":True}])
def test_request_bounds_and_overrides(extra):
    with pytest.raises(ValidationError):request(**extra)


def test_query_canonicalization_and_defaults(monkeypatch):
    result=GDELTRequest(provider="gdelt",query='  Ukraine   "foreign   policy"  ')
    assert result.query=='Ukraine "foreign policy"' and result.limit==25 and result.window_hours==24
    from app.core.config import Settings
    monkeypatch.delenv("GDELT_ENABLED",raising=False)
    assert Settings(_env_file=None).gdelt_enabled is False
    assert Settings(_env_file=None,gdelt_enabled=True).gdelt_enabled is True


def test_candidates_preserve_observations_and_provenance_without_invented_dates():
    result=parse_gdelt(document(article(),article(title="Second observation")),request())
    assert result.status=="success" and len(result.candidates)==2 and result.duplicate_observations==1
    first=result.candidates[0]
    assert first.url=="https://example.com/report" and first.publisher==first.domain=="example.com"
    assert first.published_at is None and first.snippet is None
    assert first.provenance.seen_at==datetime(2026,11,6,12,tzinfo=timezone.utc)
    assert first.provenance.original_url.endswith("?utm_source=gdelt")
    assert first.provenance.reported_domain=="reported.example" and first.provenance.source_country=="United States"
    assert first.provenance.mobile_url=="https://m.example.com/report"
    assert result.candidates[1].provenance.entry_index==1
    assert not result.limit_reached


def test_empty_is_distinct_from_invalid_entries_and_warnings():
    assert parse_gdelt(document(),request()).status=="success"
    result=parse_gdelt(document(article(url="http://127.0.0.1/"),article(seendate="bad",socialimage="http://169.254.169.254/")),request())
    assert result.status=="incomplete" and result.invalid_candidates==1 and len(result.candidates)==1
    assert result.candidates[0].provenance.warnings==["invalid_seen_date","invalid_image_url"]
    assert result.candidates[0].provenance.seen_at is None


@pytest.mark.parametrize("data",[b'',b'not json',b'[]',b'null',b'{}',b'{"articles":null}',b'{"articles":{}}',
    b'{"articles":[],"articles":[]}', b'{"articles":[],"x":NaN}', b'{"articles":[],"x":1e9999}',b'\xff'])
def test_malformed_json_never_becomes_empty(data):
    with pytest.raises(GDELTFault) as error:parse_gdelt(data,request())
    assert error.value.status=="malformed_response"


@pytest.mark.parametrize("data",[b' '* (1024*1024+1),b'['*33+b']'*33,
    b'{"articles":[],"x":"'+b'x'*65537+b'"}',document(*[article()]*26),
    b'{"articles":[],"x":'+b'1'*129+b'}',json.dumps({str(i):i for i in range(65)}).encode()])
def test_parser_resource_limits(data):
    with pytest.raises(GDELTFault) as error:parse_gdelt(data,request())
    assert error.value.status=="parser_limit"


@pytest.mark.parametrize("row",[None,[],article(title=None),article(title=""),article(title="x"*1001),
    article(url="https://example.com/"+"x"*2048),article(language="x"*65),article(url="https://user:secret@example.com")])
def test_invalid_record_is_reported(row):
    result=parse_gdelt(document(row),request())
    assert result.status=="incomplete" and result.invalid_candidates==1 and result.candidates==[]


class Body(httpx.AsyncByteStream):
    def __init__(self,data):self.data=data;self.closed=False
    async def __aiter__(self):yield self.data
    async def aclose(self):self.closed=True


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding",["identity","gzip","deflate"])
async def test_fixed_endpoint_credentials_proxy_and_bounded_compression(monkeypatch,encoding):
    monkeypatch.setenv("HTTPS_PROXY","http://private-proxy.invalid")
    data=document(article())
    wire=gzip.compress(data) if encoding=="gzip" else zlib.compress(data) if encoding=="deflate" else data
    body=Body(wire);calls=[]
    def respond(outbound):
        calls.append(outbound)
        assert outbound.url.scheme=="https" and outbound.url.host=="api.gdeltproject.org"
        assert outbound.url.params["query"]=='Ukraine "foreign policy"' and outbound.url.params["timespan"]=="24h"
        assert outbound.url.params["maxrecords"]=="25" and outbound.url.params["mode"]=="ArtList"
        assert "x-api-key" not in outbound.headers and "authorization" not in outbound.headers and "cookie" not in outbound.headers
        assert outbound.extensions["timeout"]=={"connect":5.,"read":10.,"write":10.,"pool":2.}
        return httpx.Response(200,headers={"content-type":"application/json","content-encoding":encoding,
            "content-length":str(len(wire))},stream=body)
    assert await SecureGDELTFetcher(httpx.MockTransport(respond)).fetch(request())==data
    assert len(calls)==1 and body.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,expected",[("declared","response_too_large"),("stream","response_too_large"),
    ("bomb","response_too_large"),("incomplete","malformed_response"),("trailing","malformed_response"),
    ("badzip","malformed_response"),("length","malformed_response"),("encoding","unsupported_encoding"),
    ("ctype","malformed_response"),("redirect","upstream_error")])
async def test_fetch_failures_close_without_retry(kind,expected):
    headers={"content-type":"application/json"};data=document();code=200
    if kind=="declared":headers["content-length"]=str(1024*1024+1)
    elif kind=="stream":data=b'x'*(1024*1024+1)
    elif kind in {"bomb","incomplete","trailing","badzip"}:
        headers["content-encoding"]="gzip"
        data=gzip.compress(b'x'*(1024*1024+1)) if kind=="bomb" else gzip.compress(document())
        if kind=="incomplete":data=data[:-3]
        elif kind=="trailing":data+=gzip.compress(b'{}')
        elif kind=="badzip":data=b'not gzip'
    elif kind=="length":headers["content-length"]="999"
    elif kind=="encoding":headers["content-encoding"]="br"
    elif kind=="ctype":headers["content-type"]="text/html"
    else:code=302;headers["location"]="http://169.254.169.254/"
    body=Body(data);calls=[]
    def respond(outbound):calls.append(outbound);return httpx.Response(code,headers=headers,stream=body)
    with pytest.raises(GDELTFault) as error:await SecureGDELTFetcher(httpx.MockTransport(respond)).fetch(request())
    assert error.value.status==expected and len(calls)==1 and body.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("addresses",[("127.0.0.1",),("169.254.169.254",),("93.184.216.34","10.0.0.1")])
async def test_unsafe_dns_never_connects(monkeypatch,addresses):
    # Restore actual transport processing, retaining a fake resolver/dialer.
    monkeypatch.setattr(httpcore.AsyncConnectionPool,"handle_async_request",POOL_HANDLER)
    from app.services.article_fetch import AnyIOBackend
    dns=AsyncMock(return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,"",(a,443)) for a in addresses])
    dial=AsyncMock(side_effect=AssertionError("Unsafe socket dial"))
    monkeypatch.setattr("app.services.article_fetch.anyio.getaddrinfo",dns)
    monkeypatch.setattr(AnyIOBackend,"connect_tcp",dial)
    with pytest.raises(GDELTFault) as error:await SecureGDELTFetcher().fetch(request())
    assert error.value.status=="unsafe_destination";dial.assert_not_awaited()


@pytest.mark.asyncio
async def test_trickle_total_deadline_and_timeout_sanitization(monkeypatch):
    monkeypatch.setattr(gdelt_fetch,"FETCH_DEADLINE",0.02)
    class Slow(Body):
        async def __aiter__(self):
            while True:await asyncio.sleep(.01);yield b' '
    body=Slow(b'')
    with pytest.raises(GDELTFault) as error:
        await SecureGDELTFetcher(httpx.MockTransport(lambda _:httpx.Response(200,headers={"content-type":"application/json"},stream=body))).fetch(request())
    assert error.value.status=="timeout" and body.closed


@pytest.mark.parametrize("value,delay,hold",[(None,300,False),("bad",300,False),("-1",300,False),("0007",7,False),
    ("172800",172800,False),("999999999999999",0,True),("5\r\nx",300,False)])
def test_retry_guidance_is_validated_without_shortening(value,delay,hold):
    result=retry_policy(value,300)
    assert result.delay_seconds==delay and result.hold==hold


@pytest.mark.asyncio
async def test_disabled_touches_neither_redis_nor_network():
    admission=AsyncMock();fetcher=AsyncMock()
    result=await GDELTProvider(admission,fetcher).discover(request())
    assert result.status=="disabled";admission.acquire.assert_not_awaited();fetcher.fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault",["timeout","upstream_error","throttled","storage_unavailable"])
async def test_provider_failure_and_cleanup(fault):
    admission=AsyncMock();admission.acquire.return_value="token";admission.finish.return_value="300"
    fetcher=AsyncMock();fetcher.fetch.side_effect=GDELTFault(fault,policy=RetryPolicy(delay_seconds=300))
    result=await GDELTProvider(admission,fetcher,enabled=True).discover(request())
    assert result.status==fault
    admission.finish.assert_awaited_once_with("token",RetryPolicy(delay_seconds=300))
    fetcher.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_admission_failure_never_fetches_and_cleanup_failure_fails_closed():
    admission=AsyncMock();admission.acquire.side_effect=GDELTFault("throttled","6");fetcher=AsyncMock()
    provider=GDELTProvider(admission,fetcher,enabled=True)
    assert (await provider.discover(request())).retry_after=="6";fetcher.fetch.assert_not_awaited()
    admission.acquire.side_effect=None;admission.acquire.return_value="token"
    admission.finish.side_effect=RuntimeError("private redis details");fetcher.fetch.return_value=document()
    assert (await provider.discover(request())).status=="storage_unavailable"


@pytest.mark.asyncio
async def test_provider_deadline_releases_lease(monkeypatch):
    monkeypatch.setattr(gdelt,"OPERATION_DEADLINE",.02)
    admission=AsyncMock();admission.acquire.return_value="token";fetcher=AsyncMock()
    async def slow(_):await asyncio.sleep(1)
    fetcher.fetch.side_effect=slow
    assert (await GDELTProvider(admission,fetcher,enabled=True).discover(request())).status=="timeout"
    admission.finish.assert_awaited_once()


@pytest.mark.asyncio
async def test_real_transport_pins_ip_and_retains_gdelt_tls_identity(monkeypatch):
    from app.services.article_fetch import AnyIOBackend
    monkeypatch.setattr(httpcore.AsyncConnectionPool,"handle_async_request",POOL_HANDLER)
    dns=AsyncMock(side_effect=[[(socket.AF_INET,socket.SOCK_STREAM,6,"",("93.184.216.34",443))],
                              [(socket.AF_INET,socket.SOCK_STREAM,6,"",("127.0.0.1",443))]])
    class Wire(httpcore.AsyncNetworkStream):
        def __init__(self):
            self.data=b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 15\r\n\r\n{"articles":[]}'
            self.writes=[];self.hostname=None
        async def read(self,max_bytes,timeout=None):
            data,self.data=self.data[:max_bytes],self.data[max_bytes:];return data
        async def write(self,buffer,timeout=None):self.writes.append(buffer)
        async def start_tls(self,ssl_context,server_hostname=None,timeout=None):
            assert ssl_context.check_hostname and ssl_context.verify_mode==ssl.CERT_REQUIRED
            self.hostname=server_hostname;return self
        async def aclose(self):pass
    wire=Wire();dial=AsyncMock(return_value=wire)
    monkeypatch.setattr("app.services.article_fetch.anyio.getaddrinfo",dns)
    monkeypatch.setattr(AnyIOBackend,"connect_tcp",dial)
    assert await SecureGDELTFetcher().fetch(request())==b'{"articles":[]}'
    assert dns.await_count==1 and dial.call_args.args[0]=="93.184.216.34"
    assert wire.hostname=="api.gdeltproject.org" and b'Host: api.gdeltproject.org' in b''.join(wire.writes)


@pytest.mark.asyncio
async def test_local_capacity_has_no_unbounded_wait_queue():
    gate=AsyncMock();gate.acquire.return_value="test-token";fetcher=AsyncMock()
    started=asyncio.Event();release=asyncio.Event();arrived=0
    async def wait(_):
        nonlocal arrived
        arrived+=1
        if arrived==2:started.set()
        await release.wait();return document()
    fetcher.fetch.side_effect=wait
    provider=GDELTProvider(gate,fetcher,enabled=True)
    tasks=[asyncio.create_task(provider.discover(request())) for _ in range(2)]
    await started.wait()
    assert (await provider.discover(request())).status=="busy" and gate.acquire.await_count==2
    release.set();assert all(result.status=="success" for result in await asyncio.gather(*tasks))


@pytest.mark.parametrize("value",["Fri, 06 Nov 2026 12:00:00 GMT","Thu, 01 Jan 1970 00:00:00 GMT"])
def test_http_date_guidance_is_absolute_not_worker_relative(value):
    result=retry_policy(value,300)
    assert result.delay_seconds==0 and result.until_ms>=0 and not result.hold


@pytest.mark.asyncio
@pytest.mark.parametrize("fault,expected",[("incomplete","malformed_response"),("trailing","malformed_response"),
                                          ("bomb","response_too_large")])
async def test_deflate_integrity_and_expansion(fault,expected):
    data=zlib.compress(b'x'*(1024*1024+1)) if fault=="bomb" else zlib.compress(document())
    if fault=="incomplete":data=data[:-2]
    if fault=="trailing":data+=zlib.compress(b'{}')
    body=Body(data)
    transport=httpx.MockTransport(lambda _:httpx.Response(200,headers={
        "content-type":"application/json","content-encoding":"deflate"},stream=body))
    with pytest.raises(GDELTFault) as error:await SecureGDELTFetcher(transport).fetch(request())
    assert error.value.status==expected and body.closed
