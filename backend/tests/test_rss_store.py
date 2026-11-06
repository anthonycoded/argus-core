"""Redis atomicity tests require an explicitly isolated, nonpersistent instance."""

import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app.services.discovery.rss_contracts import RSSFault
from app.services.discovery.rss_store import RedisSnapshotStore, StoredSnapshot, MAX_SNAPSHOT_BYTES


@pytest_asyncio.fixture
async def store():
    url = os.getenv("ARGUS_RSS_TEST_REDIS_URL")
    if not url:pytest.skip("Set ARGUS_RSS_TEST_REDIS_URL to an isolated nonpersistent Redis")
    parsed = urlsplit(url)
    assert parsed.hostname == "127.0.0.1" and parsed.port not in {None,6379}, "Refusing non-test Redis"
    client = Redis.from_url(url, decode_responses=True)
    assert (await client.config_get("save"))["save"] == "" and (await client.config_get("appendonly"))["appendonly"] == "no"
    assert not await client.keys("argus:discovery:rss:v1:*")
    yield RedisSnapshotStore(client)
    keys = await client.keys("argus:discovery:rss:v1:*")
    if keys:await client.delete(*keys)
    assert not await client.keys("argus:discovery:rss:v1:*")
    await client.aclose()


def snapshot(feed_id="bbc-world", title="Feed"):
    now = datetime.now(timezone.utc)
    return StoredSnapshot(snapshot_id=secrets.token_hex(16),feed_id=feed_id,secret=secrets.token_hex(32),
        feed_label="Fixture feed",feed_url="https://example.com/rss",feed_revision="1",
        fetched_at=now,expires_at=now+timedelta(seconds=300),feed_title=title,format="rss2",observations=[],duplicate_observations=0)


@pytest.mark.asyncio
async def test_atomic_lease_publication_and_immutability(store):
    assert await store.acquire("bbc-world","first")
    assert not await store.acquire("bbc-world","second")
    await store.release("bbc-world","second")
    assert not await store.acquire("bbc-world","second")
    with pytest.raises(RSSFault):await store.publish(snapshot(),"second")
    original = snapshot();await store.publish(original,"first")
    with pytest.raises(RSSFault):await store.publish(snapshot(title="Replacement"),"first")
    assert (await store.load("bbc-world")).snapshot_id == original.snapshot_id
    assert 0 < await store.client.ttl(store.key("bbc-world","snapshot")) <= 300
    await store.release("bbc-world","first")
    assert await store.acquire("bbc-world","second")


@pytest.mark.asyncio
async def test_slots_expiration_cooldown_and_byte_cap(store):
    for feed_id in ("bbc-world","npr-politics"):
        assert await store.acquire(feed_id,"token")
        await store.publish(snapshot(feed_id),"token")
        await store.release(feed_id,"token")
    assert len(await store.client.keys("argus:discovery:rss:v1:*:snapshot")) == 2
    import anyio
    await store.client.pexpire(store.key("bbc-world","snapshot"),1)
    await anyio.sleep(0.02)
    assert await store.load("bbc-world") is None
    await store.defer("bbc-world",999999)
    assert 0 < await store.cooldown("bbc-world") <= 86400
    assert await store.acquire("bbc-world","token")
    with pytest.raises(RSSFault) as error:await store.publish(snapshot(title="x" * MAX_SNAPSHOT_BYTES),"token")
    assert error.value.status == "parser_limit" and await store.load("bbc-world") is None


@pytest.mark.asyncio
async def test_registry_keys_and_corrupt_payload_fail_closed(store):
    with pytest.raises(RSSFault):await store.load("arbitrary-feed")
    await store.client.set(store.key("bbc-world","snapshot"),"invalid JSON",ex=30)
    with pytest.raises(RSSFault) as error:await store.load("bbc-world")
    assert error.value.status == "storage_unavailable"
