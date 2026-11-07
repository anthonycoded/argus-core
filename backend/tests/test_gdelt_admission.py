"""Atomic admission tests require independently verified disposable Redis."""

import asyncio
import os
from urllib.parse import urlsplit
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import httpx
from redis.asyncio import Redis
from redis.exceptions import ConnectionError

from app.services.discovery import gdelt_admission as module
from app.services.discovery.gdelt import GDELTProvider
from app.services.discovery.gdelt_admission import RedisGDELTAdmission, STATE_KEY, LEASE_KEY
from app.services.discovery.gdelt_contracts import GDELTFault, GDELTRequest, RetryPolicy


@pytest_asyncio.fixture
async def redis():
    url=os.getenv("ARGUS_GDELT_TEST_REDIS_URL");expected=os.getenv("ARGUS_GDELT_TEST_REDIS_RUN_ID")
    if not url or not expected:pytest.skip("Requires verified disposable, nonpersistent GDELT test Redis")
    parsed=urlsplit(url)
    assert parsed.hostname=="127.0.0.1" and parsed.port not in {None,6379} and parsed.path=="/0"
    client=Redis.from_url(url,decode_responses=True)
    assert (await client.info("server"))["run_id"]==expected
    assert (await client.config_get("save"))["save"]=="" and (await client.config_get("appendonly"))["appendonly"]=="no"
    assert not await client.exists(STATE_KEY,LEASE_KEY)
    try:yield client
    finally:
        assert (await client.info("server"))["run_id"]==expected
        await client.delete(STATE_KEY,LEASE_KEY)
        assert not await client.exists(STATE_KEY,LEASE_KEY)
        await client.aclose()


@pytest.mark.asyncio
async def test_redis_time_spacing_and_single_inflight(redis):
    gate=RedisGDELTAdmission(redis);before=await redis.time()
    token=await gate.acquire()
    next_ms=int(await redis.hget(STATE_KEY,"next"))
    assert module.START_GAP_MS==6000 and next_ms>=before[0]*1000+6000
    assert 0<await redis.pttl(LEASE_KEY)<=40000 and await redis.get(LEASE_KEY)==token
    with pytest.raises(GDELTFault) as error:await RedisGDELTAdmission(redis).acquire()
    assert error.value.status=="throttled" and 1<=int(error.value.retry_after)<=6
    # Advance only test-owned state to exercise the lease after spacing elapsed.
    await redis.hset(STATE_KEY,"next",0)
    with pytest.raises(GDELTFault) as error:await gate.acquire()
    assert error.value.status=="busy"
    await gate.finish(token,RetryPolicy())
    assert not await redis.exists(LEASE_KEY)


@pytest.mark.asyncio
async def test_atomic_admission_across_independent_workers(redis):
    gates=[RedisGDELTAdmission(redis) for _ in range(12)]
    results=await asyncio.gather(*(gate.acquire() for gate in gates),return_exceptions=True)
    tokens=[result for result in results if isinstance(result,str)]
    assert len(tokens)==1 and len([r for r in results if isinstance(r,GDELTFault)])==11
    await gates[0].finish(tokens[0],RetryPolicy())


@pytest.mark.asyncio
async def test_cooldown_race_cannot_launch_a_second_request(redis):
    first,second=RedisGDELTAdmission(redis),RedisGDELTAdmission(redis)
    token=await first.acquire()
    await redis.hset(STATE_KEY,"next",0)
    with pytest.raises(GDELTFault):await second.acquire()  # No sleeping waiter exists.
    retry=await first.finish(token,RetryPolicy(delay_seconds=300))
    assert 299<=int(retry)<=300
    with pytest.raises(GDELTFault) as error:await second.acquire()
    assert error.value.status=="throttled" and 299<=int(error.value.retry_after)<=300
    assert not await redis.exists(LEASE_KEY)


@pytest.mark.asyncio
async def test_monotonic_extension_and_stale_owner_never_releases_new_lease(redis):
    gate=RedisGDELTAdmission(redis);token=await gate.acquire()
    await gate.finish(token,RetryPolicy(delay_seconds=172800))
    until=int(await redis.hget(STATE_KEY,"cooldown"))
    await redis.set(LEASE_KEY,"new-owner",px=40000)
    with pytest.raises(GDELTFault):await gate.finish(token,RetryPolicy(delay_seconds=1))
    assert await redis.get(LEASE_KEY)=="new-owner" and int(await redis.hget(STATE_KEY,"cooldown"))>=until
    with pytest.raises(GDELTFault):await gate.finish(token,RetryPolicy(delay_seconds=200000))
    assert int(await redis.hget(STATE_KEY,"cooldown"))>until


@pytest.mark.asyncio
async def test_http_date_uses_server_clock_and_extreme_guidance_pauses(redis):
    gate=RedisGDELTAdmission(redis);token=await gate.acquire();seconds,micros=await redis.time()
    target=seconds*1000+micros//1000+60000
    retry=await gate.finish(token,RetryPolicy(until_ms=target))
    assert 59<=int(retry)<=60
    assert int(await redis.hget(STATE_KEY,"cooldown"))>=target
    # A late response may only extend protection, even after its lease was lost.
    with pytest.raises(GDELTFault):await gate.finish(token,RetryPolicy(hold=True))
    with pytest.raises(GDELTFault) as error:await gate.acquire()
    assert error.value.status=="paused" and error.value.retry_after is None


@pytest.mark.asyncio
async def test_lease_expiration_and_cancellation_release(redis,monkeypatch):
    gate=RedisGDELTAdmission(redis);token=await gate.acquire()
    await redis.pexpire(LEASE_KEY,1);await redis.hset(STATE_KEY,"next",0);await asyncio.sleep(.02)
    next_token=await gate.acquire();assert next_token!=token
    await gate.finish(next_token,RetryPolicy())
    await redis.hset(STATE_KEY,"next",0)
    started=asyncio.Event();fetcher=AsyncMock()
    async def wait(_):started.set();await asyncio.sleep(10)
    fetcher.fetch.side_effect=wait
    task=asyncio.create_task(GDELTProvider(gate,fetcher,enabled=True).discover(GDELTRequest(provider="gdelt",query="Ukraine")))
    await started.wait();task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    assert not await redis.exists(LEASE_KEY)


@pytest.mark.asyncio
async def test_redis_errors_and_corrupt_state_fail_closed(redis,monkeypatch):
    fetcher=AsyncMock();gate=RedisGDELTAdmission(redis)
    await redis.hset(STATE_KEY,"next","corrupt")
    result=await GDELTProvider(gate,fetcher,enabled=True).discover(GDELTRequest(provider="gdelt",query="Ukraine"))
    assert result.status=="storage_unavailable";fetcher.fetch.assert_not_awaited()
    monkeypatch.setattr(redis,"eval",AsyncMock(side_effect=ConnectionError("private redis details")))
    result=await GDELTProvider(gate,fetcher,enabled=True).discover(GDELTRequest(provider="gdelt",query="Ukraine"))
    assert result.status=="storage_unavailable" and "private" not in result.model_dump_json()
    fetcher.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_throttle_is_shared_and_no_second_fetch(redis):
    gate=RedisGDELTAdmission(redis);fetcher=AsyncMock()
    fetcher.fetch.side_effect=GDELTFault("throttled",policy=RetryPolicy(delay_seconds=60))
    req=GDELTRequest(provider="gdelt",query="Ukraine")
    first=await GDELTProvider(gate,fetcher,enabled=True).discover(req)
    second=await GDELTProvider(RedisGDELTAdmission(redis),fetcher,enabled=True).discover(req)
    assert first.status==second.status=="throttled" and 59<=int(second.retry_after)<=60
    fetcher.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_http_retry_after_drives_real_shared_cooldown(redis):
    from app.services.discovery.gdelt_fetch import SecureGDELTFetcher
    calls=[]
    def respond(request):
        calls.append(request)
        return httpx.Response(429,headers={"Retry-After":"120"},content=b'private response')
    fetcher=SecureGDELTFetcher(httpx.MockTransport(respond))
    first=await GDELTProvider(RedisGDELTAdmission(redis),fetcher,enabled=True).discover(
        GDELTRequest(provider="gdelt",query="Ukraine"))
    second=await GDELTProvider(RedisGDELTAdmission(redis),fetcher,enabled=True).discover(
        GDELTRequest(provider="gdelt",query="NATO"))
    assert first.status==second.status=="throttled" and 119<=int(second.retry_after)<=120
    assert len(calls)==1 and "private" not in first.model_dump_json()
