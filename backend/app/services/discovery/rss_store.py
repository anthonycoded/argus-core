"""Two fixed Redis snapshot slots, no caller-controlled cache keys or eviction."""

from datetime import datetime
from typing import Protocol

import anyio
from pydantic import BaseModel, Field, ValidationError
from redis.exceptions import RedisError

from app.services.discovery.feed_registry import APPROVED_FEEDS
from app.services.discovery.rss_contracts import RSSCandidate, RSSRejectedEntry, RSSFault

SNAPSHOT_TTL = 300
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_SLOTS = 2
STORAGE_FEED_IDS = frozenset({"bbc-world", "npr-politics"})


class StoredSnapshot(BaseModel):
    snapshot_id: str
    feed_id: str
    feed_label: str
    feed_url: str
    feed_revision: str
    secret: str = Field(repr=False)
    fetched_at: datetime
    expires_at: datetime
    feed_title: str | None
    format: str
    observations: list[RSSCandidate | RSSRejectedEntry] = Field(max_length=500)
    duplicate_observations: int


class SnapshotStore(Protocol):
    async def load(self, feed_id: str) -> StoredSnapshot | None: ...
    async def acquire(self, feed_id: str, token: str) -> bool: ...
    async def release(self, feed_id: str, token: str) -> None: ...
    async def publish(self, snapshot: StoredSnapshot, token: str) -> None: ...
    async def cooldown(self, feed_id: str) -> int: ...
    async def defer(self, feed_id: str, seconds: int) -> None: ...


RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""
PUBLISH = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return -1 end
if redis.call('EXISTS', KEYS[2]) == 1 then return 0 end
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
return 1
"""


class RedisSnapshotStore:
    def __init__(self, client):
        self.client = client

    def key(self, feed_id: str, kind: str) -> str:
        if len(APPROVED_FEEDS) > MAX_SLOTS or feed_id not in STORAGE_FEED_IDS or feed_id not in APPROVED_FEEDS:
            raise RSSFault("storage_unavailable")
        return f"argus:discovery:rss:v1:{feed_id}:{kind}"

    async def call(self, method, *args, **kwargs):
        try:
            with anyio.fail_after(2):
                return await method(*args, **kwargs)
        except (RedisError, TimeoutError):
            raise RSSFault("storage_unavailable") from None

    async def load(self, feed_id):
        raw = await self.call(self.client.get, self.key(feed_id, "snapshot"))
        if raw is None:
            return None
        try:
            if len(raw) > MAX_SNAPSHOT_BYTES or len(raw.encode("utf-8") if isinstance(raw, str) else raw) > MAX_SNAPSHOT_BYTES:
                raise ValueError
            snapshot = StoredSnapshot.model_validate_json(raw)
            if snapshot.feed_id != feed_id:
                raise ValueError
            return snapshot
        except (ValueError, ValidationError):
            raise RSSFault("storage_unavailable") from None

    async def acquire(self, feed_id, token):
        return bool(await self.call(self.client.set, self.key(feed_id, "lease"), token, nx=True, ex=30))

    async def release(self, feed_id, token):
        await self.call(self.client.eval, RELEASE, 1, self.key(feed_id, "lease"), token)

    async def publish(self, snapshot, token):
        raw = snapshot.model_dump_json()
        if len(raw.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise RSSFault("parser_limit")
        result = await self.call(self.client.eval, PUBLISH, 2,
            self.key(snapshot.feed_id, "lease"), self.key(snapshot.feed_id, "snapshot"), token, raw, SNAPSHOT_TTL)
        if result != 1:
            raise RSSFault("busy", "1")

    async def cooldown(self, feed_id):
        return max(0, await self.call(self.client.ttl, self.key(feed_id, "cooldown")))

    async def defer(self, feed_id, seconds):
        await self.call(self.client.set, self.key(feed_id, "cooldown"), "1", ex=max(1, min(seconds, 86400)))
