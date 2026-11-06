"""Offline RSS wire, pagination and editorial integration regressions."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import feedparser
import httpcore
import httpx
import pytest

from app.clients.argus_core_client import ArgusCoreClient, CoreClientError, RSSDiscoveryError
from app.services import research_service


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(httpcore.AsyncConnectionPool, "handle_async_request", AsyncMock(side_effect=AssertionError("Live HTTP")))
    monkeypatch.setattr(feedparser, "parse", Mock(side_effect=AssertionError("Local RSS fallback")))
    monkeypatch.setattr(research_service, "discover_gdelt_sources", AsyncMock(side_effect=AssertionError("GDELT")))


def candidate(index, title="Unrelated supplied report", url=None):
    url = url or f"https://example.com/{index}"
    return dict(provider="rss", source_type="rss", title=title, url=url, canonical_url=url,
                domain="example.com", publisher="Provider publisher", published_at="2026-11-06T12:00:00+02:00",
                snippet="Supplied summary", provenance=dict(entry_index=index, original_url=f"/original/{index}",
                provider_item_id=str(index), published_raw="Supplied date", date_field="pubDate",
                reported_publisher="Reported publisher", warnings=[]))


def page(offset=0, total=76, feed_id="bbc-world"):
    end = min(offset + 75, total)
    return dict(provider="rss", feed_id=feed_id, feed_label="BBC World", feed_url="https://feeds.bbci.co.uk/news/world/rss.xml",
                feed_revision="1", feed_title="Supplied feed", format="rss2", status="success",
                candidates=[candidate(i) for i in range(offset, end)], rejected_entries=[], snapshot_id="a" * 32,
                fetched_at="2026-11-06T10:00:00Z", expires_at="2026-11-06T10:05:00Z", total_entries=total,
                invalid_entries=0, duplicate_observations=0, offset=offset, entries_in_page=end-offset,
                has_more=end < total, next_cursor=f"cursor-{end}" if end < total else None, error=None, retry_after=None)


def client(http):
    return ArgusCoreClient(base_url="https://core.test", api_key="test-only-key", request_timeout=4, http_client=http)


@pytest.mark.asyncio
async def test_authenticated_all_pages_order_duplicates_and_adapter():
    first, second = page(), page(75)
    first["candidates"][1] = candidate(1, url=first["candidates"][0]["url"])
    first["duplicate_observations"] = second["duplicate_observations"] = 1
    requests = []
    def respond(request):
        payload = json.loads(request.content);requests.append(payload)
        assert request.method == "POST" and request.url.path == "/api/v1/discovery"
        assert request.headers["X-API-Key"] == "test-only-key"
        assert all(t == 4 for t in request.extensions["timeout"].values())
        return httpx.Response(200, json=first if payload["cursor"] is None else second)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        sources = await client(http).discover_rss(feed_id="bbc-world")
    assert requests == [dict(provider="rss", feed_id="bbc-world", limit=75, cursor=None),
                        dict(provider="rss", feed_id="bbc-world", limit=75, cursor="cursor-75")]
    assert len(sources) == 76 and sources[0]["url"] == sources[1]["url"]
    assert [s["provider_metadata"]["provenance"]["entry_index"] for s in sources] == list(range(76))
    assert sources[0]["published_at"] == datetime(2026, 11, 6, 10, tzinfo=timezone.utc)
    assert sources[0]["provider_metadata"]["provenance"]["original_url"] == "/original/0"
    assert sources[0]["snippet"] == "Supplied summary"
    assert sources[0]["provider_metadata"]["publisher"] == "Provider publisher"


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(401,502),(403,502),(410,410),(429,429),(503,503),(504,504),(400,502)])
async def test_later_page_failure_retains_evidence_without_retry(status, expected):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=page()) if len(calls) == 1 else httpx.Response(status, json={"secret": "must not escape"}, headers={"Retry-After":"007"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(RSSDiscoveryError) as error:
            await client(http).discover_rss(feed_id="bbc-world")
    assert error.value.status_code == expected and len(error.value.partial_sources) == 75 and len(calls) == 2
    assert error.value.retry_after == ("7" if status in {429,503} else None)
    assert "must not escape" not in str(error.value)


@pytest.mark.asyncio
async def test_timeout_and_invalid_retry_after():
    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) == 1:return httpx.Response(200, json=page())
        raise httpx.ReadTimeout("Sensitive provider detail")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(RSSDiscoveryError) as error:await client(http).discover_rss(feed_id="bbc-world")
    assert error.value.status_code == 504 and len(error.value.partial_sources) == 75 and len(calls) == 2
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(429, headers={"Retry-After":"not-a-date"}))) as http:
        with pytest.raises(RSSDiscoveryError) as error:await client(http).discover_rss(feed_id="bbc-world")
    assert error.value.retry_after is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    {"snapshot_id":"b"*32}, {"feed_id":"npr-politics"}, {"offset":74}, {"total_entries":77},
    {"feed_revision":"2"}, {"has_more":True,"next_cursor":"cursor-75"},
    {"entries_in_page":0}, {"invalid_entries":1}, {"expires_at":"2026-11-06T10:00:00Z"},
])
async def test_inconsistent_snapshot_rejected_without_accepting_bad_page(mutation):
    second = page(75);second.update(mutation)
    responses = iter([page(), second]);calls=[]
    def respond(request):calls.append(request);return httpx.Response(200,json=next(responses))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(RSSDiscoveryError) as error:await client(http).discover_rss(feed_id="bbc-world")
    assert len(error.value.partial_sources) == 75 and len(calls) == 2 and error.value.status_code == 502


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"url":"http://127.0.0.1/private"}, {"published_at":"2026-11-06T10:00:00"},
    {"published_at":12345}, {"title":""}, {"url":"https://example.com/bad%ZZ"},
])
async def test_malformed_candidate_is_explicit_failure(change):
    result=page(total=1);result["candidates"][0].update(change)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=result))) as http:
        with pytest.raises(RSSDiscoveryError) as error:await client(http).discover_rss(feed_id="bbc-world")
    assert error.value.status_code == 502 and error.value.partial_sources == []


@pytest.mark.asyncio
@pytest.mark.parametrize("total", [0,500,501])
async def test_empty_and_bounded_full_feed(total):
    calls=[]
    def respond(request):
        offset=len(calls)*75;calls.append(request)
        return httpx.Response(200,json=page(offset,total))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        if total > 500:
            with pytest.raises(RSSDiscoveryError):await client(http).discover_rss(feed_id="bbc-world")
            assert len(calls)==1
        else:
            assert len(await client(http).discover_rss(feed_id="bbc-world"))==total
            assert len(calls)==max(1,(total+74)//75)


@pytest.mark.asyncio
async def test_repeated_cursor_with_remaining_pages():
    first=page(total=151);second=page(75,151);second["next_cursor"]="cursor-75"
    responses=iter([first,second])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,json=next(responses)))) as http:
        with pytest.raises(RSSDiscoveryError) as error:await client(http).discover_rss(feed_id="bbc-world")
    assert len(error.value.partial_sources)==75


@pytest.mark.asyncio
async def test_later_page_filtering_winner_selection_and_feed_order(monkeypatch):
    calls=[]
    def respond(request):
        payload=json.loads(request.content);calls.append(payload)
        if payload["feed_id"]=="npr-politics":return httpx.Response(200,json=page(total=0,feed_id="npr-politics"))
        result=page(75 if payload["cursor"] else 0)
        if payload["cursor"]:
            result["candidates"][0]=candidate(75,"Ukraine NATO tensions talks", "https://example.com/shared")
        else:
            result["candidates"][0]=candidate(0,"Ukraine NATO tensions", "https://example.com/shared")
        return httpx.Response(200,json=result)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        selected=await research_service.discover_rss_sources("Ukraine NATO tensions talks",1,discovery_client=client(http))
    assert selected[0]["title"]=="Ukraine NATO tensions talks" and selected[0]["publisher"]=="BBC World"
    assert selected[0]["provider_metadata"]["provenance"]["entry_index"]==75
    assert [(p["feed_id"],p["cursor"]) for p in calls]==[("bbc-world",None),("bbc-world","cursor-75"),("npr-politics",None)]


@pytest.mark.asyncio
async def test_partial_rss_coverage_retains_selected_evidence_and_google(monkeypatch):
    source=page(total=1);source["candidates"][0]=candidate(0,"Ukraine NATO tensions")
    source["status"]="incomplete";source["candidates"][0]["provenance"]["warnings"]=["missing_date"]
    source["candidates"][0]["published_at"]=None
    def respond(request):
        payload=json.loads(request.content)
        if payload["provider"]=="google_news":
            return httpx.Response(200,json=dict(provider="google_news",status="success",candidates=[],invalid_candidates=0,duplicates=0,truncated=False,error=None,retry_after=None))
        return httpx.Response(200,json=source) if payload["feed_id"]=="bbc-world" else httpx.Response(503)
    monkeypatch.setattr(research_service,"classify_article_relevance",AsyncMock(return_value=SimpleNamespace(classification="relevant")))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(research_service.ResearchDiscoveryError) as error:
            await research_service.discover_sources("Ukraine NATO tensions",discovery_client=client(http))
    assert len(error.value.partial_sources)==1 and error.value.partial_sources[0]["published_at"] is None
    assert "incomplete" in error.value.detail and "_relevance" not in error.value.partial_sources[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", ["bbc_world.xml","npr_politics.xml"])
async def test_real_core_wire_contract_with_mocked_provider(fixture):
    # Contract test uses Core parser and response models, with no network/store.
    from app.services.discovery.feed_registry import approved_feed
    from app.services.discovery.rss_parser import parse_rss
    from app.services.discovery.rss_contracts import RSSCandidate, RSSDiscoveryResult
    feed_id="bbc-world" if fixture.startswith("bbc") else "npr-politics"
    feed=approved_feed(feed_id)
    data=(Path(__file__).parent/"fixtures"/"discovery"/fixture).read_bytes()
    title,format_name,observations,duplicates=parse_rss(data,feed)
    candidates=[c for c in observations if isinstance(c,RSSCandidate)]
    rejected=[c for c in observations if not isinstance(c,RSSCandidate)]
    result=RSSDiscoveryResult(feed_id=feed_id,feed_label=feed.label,feed_url=feed.url,feed_revision=feed.revision,
        feed_title=title,format=format_name,status="incomplete",snapshot_id="c"*32,
        fetched_at="2026-11-06T10:00:00Z",expires_at="2026-11-06T10:05:00Z",
        candidates=candidates,rejected_entries=rejected,total_entries=len(observations),invalid_entries=len(rejected),
        duplicate_observations=duplicates,entries_in_page=len(observations))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,content=result.model_dump_json()))) as http:
        with pytest.raises(RSSDiscoveryError) as error:await client(http).discover_rss(feed_id=feed_id)
    assert len(error.value.partial_sources)==len(candidates)
    assert [s["provider_metadata"]["provenance"]["entry_index"] for s in error.value.partial_sources]==[c.provenance.entry_index for c in candidates]


@pytest.mark.asyncio
async def test_collection_total_deadline_keeps_prior_pages(monkeypatch):
    from app.clients import argus_core_client
    monkeypatch.setattr(argus_core_client,"RSS_COLLECTION_TIMEOUT",0.1)
    calls=[]
    async def respond(request):
        calls.append(request)
        if len(calls)==1:return httpx.Response(200,json=page())
        await asyncio.sleep(1)
        pytest.fail("Collection deadline did not cancel request")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with pytest.raises(RSSDiscoveryError) as error:await client(http).discover_rss(feed_id="bbc-world")
    assert error.value.status_code==504 and len(error.value.partial_sources)==75 and len(calls)==2


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b"not json", b"x"*(4*1024*1024+1)])
async def test_malformed_or_excessive_responses(content):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:httpx.Response(200,content=content))) as http:
        with pytest.raises(RSSDiscoveryError) as error:await client(http).discover_rss(feed_id="bbc-world")
    assert error.value.status_code==502 and error.value.partial_sources==[]


@pytest.mark.asyncio
async def test_invalid_feed_id_fails_before_http():
    def forbidden(_):pytest.fail("Invalid feed ID reached HTTP")
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        with pytest.raises(CoreClientError) as error:await client(http).discover_rss(feed_id="https://arbitrary.example/rss")
    assert error.value.status_code==422


@pytest.mark.asyncio
async def test_equal_overlap_keeps_first_observation_and_feed_label():
    def respond(request):
        feed_id=json.loads(request.content)["feed_id"]
        result=page(total=2,feed_id=feed_id)
        result["candidates"]=[candidate(i,"Ukraine NATO tensions", "https://example.com/shared") for i in range(2)]
        result["duplicate_observations"]=1
        return httpx.Response(200,json=result)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        selected=await research_service.discover_rss_sources("Ukraine NATO tensions",discovery_client=client(http))
    assert len(selected)==1 and selected[0]["publisher"]=="BBC World"
    assert selected[0]["provider_metadata"]["provenance"]["entry_index"]==0
