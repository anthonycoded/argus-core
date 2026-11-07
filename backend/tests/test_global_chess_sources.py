"""Source identity contracts only: fake HTTP, no database, providers, or research writes."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpcore
import httpx
import pytest
from pydantic import ValidationError

from app.clients.argus_core_client import ArgusCoreClient, CoreSourceError
from app.clients.source_contracts import (
    PublicArticleContent, SourceRegistration, article_hash, registry_url,
)

KEY = "offline-source-client-key"
ID = "12345678-1234-4234-8234-123456789abc"
OTHER_ID = "22345678-1234-4234-8234-123456789abc"
URL = "https://example.com/article"
OBSERVED = URL + "/?utm_source=rss#observed"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", AsyncMock(
        side_effect=AssertionError("Real HTTP forbidden")))


def registration(**kwargs):
    return SourceRegistration(title="Observed title", observed_url=OBSERVED,
                              publisher="Observed publisher", **kwargs)


def full_article(text="Full public article text."):
    return PublicArticleContent(kind="full_public_article", text=text)


def facts(**kwargs):
    return {"id": ID, "title": "Canonical title", "url": URL, "canonical_url": URL,
            "domain": "example.com", "publisher": "Canonical publisher", "published_at": None,
            "source_type": "news", "content_hash": None, "metadata": {},
            "created_at": "2026-11-06T12:00:00Z", **kwargs}


def receipt(**kwargs):
    return {"status": "created", "source_id": ID, "canonical_url": URL, "content_hash": None, **kwargs}


def duplicate(reason="canonical_url", url=URL):
    return {"status": "duplicate", "source_id": ID, "canonical_url": url, "reason": reason}


@asynccontextmanager
async def client_for(responses, calls, **http_kwargs):
    def respond(request):
        calls.append(request)
        value = responses[len(calls)-1]
        if isinstance(value, Exception):
            raise value
        return value
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), **http_kwargs) as http:
        yield ArgusCoreClient(base_url="https://core.example/prefix", api_key=KEY,
                              request_timeout=2.5, http_client=http)
        assert not http.is_closed


@pytest.mark.asyncio
async def test_created_identity_preserves_observation_and_public_wire(caplog):
    calls = []
    async with client_for([httpx.Response(200, json=receipt()), httpx.Response(200, json=facts())], calls,
                          headers={"Authorization": "private-client-auth"}, cookies={"private": "cookie"},
                          auth=("private-user", "private-password"), follow_redirects=True) as client:
        result = await client.register_source(registration())
    assert result.match == "created" and not result.requires_review
    assert result.observed_url == OBSERVED and result.source.canonical_url == URL
    assert result.observed_publisher == "Observed publisher" and result.source.publisher == "Canonical publisher"
    assert result.content_verification == "not_supplied" and result.source.id == ID
    assert [r.method for r in calls] == ["POST", "GET"]
    assert [str(r.url) for r in calls] == ["https://core.example/prefix/api/v1/sources/register",
                                         "https://core.example/prefix/api/v1/sources/" + ID]
    payload = json.loads(calls[0].content)
    assert payload == {"title": "Observed title", "url": OBSERVED, "publisher": "Observed publisher",
                       "published_at": None, "source_type": "unknown"}
    for request in calls:
        assert request.headers["X-API-Key"] == KEY
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert all(t == 2.5 for t in request.extensions["timeout"].values())
        assert KEY not in str(request.url) and KEY not in request.content.decode()
    assert KEY not in caplog.text


@pytest.mark.asyncio
async def test_created_full_article_fingerprint_is_verified():
    fingerprint = article_hash("Full public article text.")
    calls = []
    async with client_for([httpx.Response(200, json=receipt(content_hash=fingerprint)),
                           httpx.Response(200, json=facts(content_hash=fingerprint))], calls) as client:
        result = await client.register_source(registration(article=full_article()))
    assert result.match == "created" and result.content_verification == "matched"
    assert json.loads(calls[0].content)["content"] == "Full public article text."


@pytest.mark.asyncio
@pytest.mark.parametrize("with_content,stored_hash,verification", [
    (False, None, "not_supplied"), (True, None, "unavailable"),
    (True, article_hash("Full public article text."), "matched"),
])
async def test_url_duplicate_is_not_a_merge_or_publisher_update(with_content, stored_hash, verification):
    calls = []
    async with client_for([httpx.Response(200, json=duplicate()),
                           httpx.Response(200, json=facts(content_hash=stored_hash))], calls) as client:
        result = await client.register_source(registration(article=full_article() if with_content else None))
    assert result.match == "url_matched" and not result.requires_review
    assert result.source.title == "Canonical title" and result.source.publisher == "Canonical publisher"
    assert result.content_verification == verification


@pytest.mark.asyncio
async def test_cross_url_content_duplicate_is_only_a_review_candidate():
    canonical = "https://original.example/story"
    fingerprint = article_hash("Full public article text.")
    calls = []
    async with client_for([httpx.Response(200, json=duplicate("content_hash", canonical)), httpx.Response(200,
            json=facts(url=canonical, canonical_url=canonical, domain="original.example", content_hash=fingerprint))], calls) as client:
        result = await client.register_source(registration(article=full_article()))
    assert result.match == "cross_url_content_matched" and result.requires_review
    assert result.observed_url == OBSERVED and result.source.canonical_url == canonical
    assert result.content_verification == "matched" and len(calls) == 2


@pytest.mark.asyncio
async def test_same_url_content_race_retains_wire_reason():
    fingerprint = article_hash("Full public article text.")
    async with client_for([httpx.Response(200, json=duplicate("content_hash")),
                           httpx.Response(200, json=facts(content_hash=fingerprint))], []) as client:
        result = await client.register_source(registration(article=full_article()))
    assert result.match == "url_matched" and result.receipt.reason == "content_hash"


@pytest.mark.asyncio
@pytest.mark.parametrize("case,kind,count", [
    ("url_hash", "url_hash_disagreement", 2), ("wrong_url", "url_disagreement", 1),
    ("unsupported_hash", "unsupported_content_match", 1),
    ("unverified_hash", "unverified_content_match", 2),
    ("created_hash", "created_identity_disagreement", 1),
    ("changed_lookup", "receipt_lookup_disagreement", 2),
])
async def test_identity_conflicts_never_merge(case, kind, count):
    wire = duplicate(); stored = facts(); entry = registration(article=full_article())
    if case == "url_hash": stored["content_hash"] = article_hash("A different full article")
    elif case == "wrong_url": wire["canonical_url"] = "https://other.example/article"
    elif case == "unsupported_hash": wire = duplicate("content_hash"); entry = registration()
    elif case == "unverified_hash": wire = duplicate("content_hash")
    elif case == "created_hash": wire = receipt()
    else: stored.update(url="https://example.com/changed", canonical_url="https://example.com/changed")
    calls = []
    async with client_for([httpx.Response(200, json=wire), httpx.Response(200, json=stored)], calls) as client:
        with pytest.raises(CoreSourceError) as error:
            await client.register_source(entry)
    assert error.value.status_code == 409 and error.value.kind == kind
    assert error.value.write_outcome == "acknowledged" and error.value.receipt.source_id == ID
    assert OBSERVED not in str(error.value) and len(calls) == count


@pytest.mark.parametrize("field", ["snippet", "metadata", "notes", "relevance", "topic_id", "content", "provider_metadata"])
def test_editorial_fields_are_rejected_not_forwarded(field):
    with pytest.raises(ValidationError) as error:
        registration(**{field: "private-editorial-value"})
    assert "private-editorial-value" not in str(error.value)


@pytest.mark.parametrize("url", ["http://localhost/a", "http://127.0.0.1/a", "https://user:password@example.com/a",
    URL + "?access_token=secret", URL + "?%61pi_key=secret", URL + "/bad%", URL + "/" + "x"*2048])
def test_unsafe_or_secret_bearing_observed_urls_are_rejected(url):
    with pytest.raises(ValidationError):
        SourceRegistration(title="Public title", observed_url=url)


def test_full_article_requires_explicit_opt_in_and_is_not_in_repr():
    with pytest.raises(ValidationError):PublicArticleContent(text="not classified as full article")
    with pytest.raises(ValidationError):full_article(" "*3)
    with pytest.raises(ValidationError):full_article("x"*250001)
    value = registration(article=full_article("Public full text supplied explicitly"))
    assert "Public full text supplied explicitly" not in repr(value)
    assert "Public full text supplied explicitly" not in repr(value.article)
    assert value.wire_payload()["content"] == "Public full text supplied explicitly"


def test_current_core_normalization_and_hash_contract_without_core_runtime_access():
    # Test-only pure helpers/schemas act as a compatibility oracle, never ORM/services.
    from app.api.v1.schemas import SourceRegistrationRequest
    from app.utils.deduplication import canonical_source_url, content_hash
    for url in [OBSERVED, "https://EXAMPLE.com:443/a/?id=2&utm_source=x&b=1#fragment", "https://example.com"]:
        request = SourceRegistrationRequest(**SourceRegistration(title="Public", observed_url=url).wire_payload())
        assert registry_url(url) == canonical_source_url(str(request.url))
    assert article_hash("  A\n Full ARTICLE. ") == content_hash("  A\n Full ARTICLE. ")
    instant = datetime(2026,11,6,12,tzinfo=timezone.utc)
    assert registration(published_at=instant).wire_payload()["published_at"] == "2026-11-06T12:00:00Z"


@pytest.mark.parametrize("date", [42, True, "2026-11-06T12:00:00"])
def test_registration_does_not_invent_publication_times(date):
    with pytest.raises(ValidationError):registration(published_at=date)


@pytest.mark.asyncio
@pytest.mark.parametrize("identifier", ["", "../sources", "id?token=x", "id/other", "id%2Fother", "x"*37, None, 42])
async def test_invalid_lookup_ids_fail_before_http(identifier):
    calls = []
    async with client_for([], calls) as client:
        with pytest.raises(CoreSourceError) as error:await client.lookup_source(identifier)
    assert error.value.kind == "input" and error.value.write_outcome == "not_attempted" and not calls


@pytest.mark.asyncio
async def test_legacy_opaque_ids_supported_and_metadata_never_exported():
    async with client_for([httpx.Response(200, json=facts(id="legacy-source", metadata={"notes": "private value"}))], []) as client:
        source = await client.lookup_source("legacy-source")
    assert source.id == "legacy-source" and "private value" not in source.model_dump_json()
    assert "metadata" not in source.model_dump()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected,kind,outcome", [
    (401,502,"authentication","rejected"), (403,502,"permission","rejected"),
    (429,429,"throttled","rejected"), (503,503,"unavailable","unknown"),
    (504,504,"timeout","unknown"), (422,422,"rejected","rejected"),
    (500,502,"upstream","unknown"), (302,502,"upstream","unknown"),
])
async def test_http_errors_are_sanitized_and_not_retried(status, expected, kind, outcome, caplog):
    calls = []
    async with client_for([httpx.Response(status, headers={"Retry-After": "17", "Location": "https://attacker.example"},
                                         json={"detail": KEY, "notes": "private value"})], calls) as client:
        with pytest.raises(CoreSourceError) as error:await client.register_source(registration())
    assert error.value.status_code == expected and error.value.kind == kind
    assert error.value.write_outcome == outcome and error.value.retry_after == ("17" if status in {429,503} else None)
    assert len(calls) == 1 and KEY not in str(error.value) and KEY not in caplog.text
    assert "private value" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("guidance,expected", [("0007","7"),("-1",None),(KEY,None),
    ("999999999999999",None),("Fri, 01 Jan 2038 00:00:00 GMT","Fri, 01 Jan 2038 00:00:00 GMT")])
async def test_retry_after_validation(guidance, expected):
    async with client_for([httpx.Response(429, headers={"Retry-After": guidance})], []) as client:
        with pytest.raises(CoreSourceError) as error:await client.register_source(registration())
    assert error.value.retry_after == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_error,status", [(httpx.ReadTimeout,504),(httpx.ConnectTimeout,504),
    (httpx.ReadError,502),(httpx.WriteError,502),(httpx.RemoteProtocolError,502)])
async def test_ambiguous_lost_write_responses_are_never_replayed(transport_error, status):
    calls = []
    async with client_for([transport_error("private diagnostic " + KEY)], calls) as client:
        with pytest.raises(CoreSourceError) as error:await client.register_source(registration())
    assert error.value.status_code == status and error.value.write_outcome == "unknown"
    assert error.value.receipt is None and len(calls) == 1 and KEY not in str(error.value)


@pytest.mark.asyncio
async def test_failed_followup_retains_receipt_for_read_only_reconciliation():
    calls = []
    async with client_for([httpx.Response(200, json=receipt()), httpx.ReadTimeout("lost lookup")], calls) as client:
        with pytest.raises(CoreSourceError) as error:await client.register_source(registration())
    assert error.value.write_outcome == "acknowledged" and error.value.receipt.source_id == ID
    assert [r.method for r in calls] == ["POST", "GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401,403,404,429,503])
async def test_verification_failures_never_replay_acknowledged_writes(status):
    calls = []
    async with client_for([httpx.Response(200, json=duplicate()),
                           httpx.Response(status, headers={"Retry-After":"23"})], calls) as client:
        with pytest.raises(CoreSourceError) as error:
            await client.register_source(registration())
    assert error.value.write_outcome == "acknowledged" and error.value.receipt.source_id == ID
    assert error.value.retry_after == ("23" if status in {429,503} else None)
    assert [request.method for request in calls] == ["POST","GET"]


@pytest.mark.asyncio
async def test_commit_then_lost_receipt_stays_unknown_until_explicit_reconciliation():
    calls = []; committed = False
    def respond(request):
        nonlocal committed
        calls.append(request)
        if request.method == "POST":
            committed = True  # Simulated server committed before the connection failed.
            raise httpx.ReadError("receipt lost after server commit", request=request)
        assert committed
        return httpx.Response(200, json=facts())
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = ArgusCoreClient(base_url="https://core.example", api_key=KEY, http_client=http)
        with pytest.raises(CoreSourceError) as error:
            await client.register_source(registration())
        assert committed and error.value.write_outcome == "unknown" and error.value.receipt is None
        assert len(calls) == 1
        # An operator-known ID can be checked explicitly; the client cannot guess it.
        assert (await client.lookup_source(ID)).id == ID
    assert [request.method for request in calls] == ["POST","GET"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"snippet":"private"}, None, {"title":"Public","observed_url":URL}])
async def test_only_explicit_typed_registrations_are_accepted(payload):
    calls = []
    async with client_for([], calls) as client:
        with pytest.raises(CoreSourceError) as error:await client.register_source(payload)
    assert error.value.kind == "input" and error.value.write_outcome == "not_attempted" and not calls


@pytest.mark.asyncio
async def test_invalid_configuration_does_not_attempt_a_write():
    client = ArgusCoreClient(base_url="https://user:private@core.example", api_key=KEY)
    with pytest.raises(CoreSourceError) as error:await client.register_source(registration())
    assert error.value.kind == "configuration" and error.value.write_outcome == "not_attempted"
    assert "private" not in str(error.value)


@pytest.mark.asyncio
async def test_normalized_url_expansion_is_rejected_before_registration():
    value = SourceRegistration(title="Public title", observed_url=URL + "/" + "\u00e9"*1000)
    calls = []
    async with client_for([], calls) as client:
        with pytest.raises(CoreSourceError) as error:await client.register_source(value)
    assert error.value.kind == "input" and error.value.write_outcome == "not_attempted" and not calls


@pytest.mark.asyncio
async def test_lookup_not_found_is_read_only():
    async with client_for([httpx.Response(404, json={"detail": "private"})], []) as client:
        with pytest.raises(CoreSourceError) as error:await client.lookup_source(ID)
    assert error.value.status_code == 404 and error.value.kind == "not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", [None, [], {}, receipt(source_id="../bad"), receipt(content_hash="bad"),
    receipt(reason="canonical_url"), {"status":"created","source_id":ID,"canonical_url":URL},
    {**duplicate(),"reason":"title"}, duplicate(url=URL+"?utm_source=bad")])
async def test_malformed_receipts_have_unknown_write_outcome(wire):
    async with client_for([httpx.Response(200, json=wire)], []) as client:
        with pytest.raises(CoreSourceError) as error:await client.register_source(registration())
    assert error.value.kind == "protocol" and error.value.write_outcome == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", [facts(id=OTHER_ID), facts(domain="other.example"), facts(canonical_url=URL+"/wrong"),
    facts(content_hash="not-a-hash"), facts(metadata=[]), facts(created_at=42), facts(publisher="x"*501)])
async def test_inconsistent_lookups_do_not_resolve_identity(wire):
    async with client_for([httpx.Response(200, json=wire)], []) as client:
        with pytest.raises(CoreSourceError) as error:await client.lookup_source(ID)
    assert error.value.kind == "protocol" and error.value.write_outcome == "not_attempted"


class Body(httpx.AsyncByteStream):
    def __init__(self, data, slow=False):self.data=data;self.slow=slow;self.closed=False
    async def __aiter__(self):
        if self.slow:await asyncio.sleep(1)
        yield self.data
    async def aclose(self):self.closed=True


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["size","length","encoding","type","json","duplicate_keys","depth"])
async def test_bounded_streaming_protocol_errors_close_response(case):
    headers={"content-type":"application/json"};data=b'{}'
    if case=="size":data=b'x'*65537
    elif case=="length":headers["content-length"]="100"
    elif case=="encoding":headers["content-encoding"]="gzip"
    elif case=="type":headers["content-type"]="text/html"
    elif case=="json":data=KEY.encode()
    elif case=="duplicate_keys":data=b'{"id":"a","id":"b"}'
    else:data=b'['*1100+b']'*1100
    body=Body(data)
    async with client_for([httpx.Response(200,headers=headers,stream=body)], []) as client:
        with pytest.raises(CoreSourceError) as error:await client.lookup_source(ID)
    assert error.value.kind=="protocol" and body.closed and KEY not in str(error.value)


@pytest.mark.asyncio
async def test_total_request_deadline_is_not_reset_by_streaming(monkeypatch):
    monkeypatch.setattr("app.clients.argus_core_client.SOURCE_REQUEST_DEADLINE",.02)
    body=Body(b'{}',slow=True)
    async with client_for([httpx.Response(200,headers={"content-type":"application/json"},stream=body)], []) as client:
        with pytest.raises(CoreSourceError) as error:await client.register_source(registration())
    assert error.value.kind=="timeout" and error.value.write_outcome=="unknown" and body.closed


@pytest.mark.asyncio
async def test_owned_client_disables_proxies_and_closes():
    http=AsyncMock();http.send.return_value=httpx.Response(200,json=facts())
    with patch("app.clients.argus_core_client.httpx.AsyncClient") as constructor:
        constructor.return_value.__aenter__.return_value=http
        await ArgusCoreClient(base_url="https://core.example",api_key=KEY).lookup_source(ID)
        constructor.assert_called_once_with(trust_env=False)
        constructor.return_value.__aexit__.assert_awaited_once()
    assert http.send.call_args.kwargs["follow_redirects"] is False
