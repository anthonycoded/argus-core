import base64
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timezone

import anyio

from app.services.discovery.contracts import DiscoveryError
from app.services.discovery.feed_registry import approved_feed
from app.services.discovery.rss_contracts import (
    RSSCandidate, RSSDiscoveryRequest, RSSDiscoveryResult, RSSFault, RSSRejectedEntry,
)
from app.services.discovery.rss_fetch import RSSFetcher, SecureRSSFetcher
from app.services.discovery.rss_parser import parse_rss
from app.services.discovery.rss_store import MAX_SNAPSHOT_BYTES, SNAPSHOT_TTL, SnapshotStore, StoredSnapshot

_capacity = anyio.CapacityLimiter(2)
OPERATION_DEADLINE = 25.0


def cursor_for(snapshot: StoredSnapshot, app_id: str, offset: int, limit: int) -> str:
    payload = json.dumps([1, snapshot.snapshot_id, snapshot.feed_id, app_id, offset, limit], separators=(",", ":")).encode()
    signature = hmac.new(bytes.fromhex(snapshot.secret), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")


def decode_cursor(cursor: str):
    try:
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        payload, signature = raw[:-32], raw[-32:]
        fields = json.loads(payload)
        if len(signature) != 32 or not isinstance(fields, list) or len(fields) != 6:
            raise ValueError
        return payload, signature, fields
    except (ValueError, TypeError, UnicodeError):
        raise RSSFault("invalid_cursor") from None


def cursor_offset(cursor: str, snapshot: StoredSnapshot, app_id: str, limit: int) -> int:
    try:
        payload, signature, fields = decode_cursor(cursor)
        version, snapshot_id, feed_id, owner, offset, size = fields
        if (version != 1 or feed_id != snapshot.feed_id or owner != app_id or size != limit
                or not isinstance(snapshot_id, str) or len(snapshot_id) != 32):
            raise ValueError
        if snapshot_id != snapshot.snapshot_id:
            raise RSSFault("cursor_expired")
        if not hmac.compare_digest(signature, hmac.new(bytes.fromhex(snapshot.secret), payload, hashlib.sha256).digest()):
            raise ValueError
        if (type(offset) is not int
                or not 0 < offset < len(snapshot.observations) or offset % limit):
            raise ValueError
        return offset
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise RSSFault("invalid_cursor") from None


class RSSProvider:
    def __init__(self, store: SnapshotStore, fetcher: RSSFetcher | None = None, clock=time.time):
        self.store = store
        self.fetcher = fetcher if fetcher is not None else SecureRSSFetcher()
        self.clock = clock

    async def discover(self, request: RSSDiscoveryRequest, app_id: str) -> RSSDiscoveryResult:
        feed = approved_feed(request.feed_id)
        if feed is None:
            return RSSFault("unknown_feed").result(request.feed_id)
        try:
            if request.cursor:
                decode_cursor(request.cursor)
            with anyio.fail_after(OPERATION_DEADLINE):
                async with _capacity:
                    snapshot = await self.store.load(feed.feed_id)
                    if snapshot is not None and snapshot.expires_at.timestamp() <= self.clock():
                        snapshot = None
                    if request.cursor:
                        if snapshot is None:
                            raise RSSFault("cursor_expired")
                        offset = cursor_offset(request.cursor, snapshot, app_id, request.limit)
                    else:
                        offset = 0
                        if snapshot is None:
                            cooldown = await self.store.cooldown(feed.feed_id)
                            if cooldown:
                                raise RSSFault("throttled", str(cooldown))
                            token = secrets.token_hex(16)
                            if not await self.store.acquire(feed.feed_id, token):
                                raise RSSFault("busy", "1")
                            try:
                                snapshot = await self.store.load(feed.feed_id)
                                if snapshot is not None and snapshot.expires_at.timestamp() <= self.clock():
                                    raise RSSFault("busy", "1")
                                if snapshot is None:
                                    fetched = await self.fetcher.fetch(feed)
                                    fetched_at = datetime.fromtimestamp(self.clock(), timezone.utc)
                                    title, format_name, observations, duplicates = await anyio.to_thread.run_sync(parse_rss, fetched.data, feed)
                                    await anyio.lowlevel.checkpoint()
                                    snapshot = StoredSnapshot(snapshot_id=secrets.token_hex(16), feed_id=feed.feed_id,
                                        feed_label=feed.label, feed_url=feed.url, feed_revision=feed.revision,
                                        secret=secrets.token_hex(32), fetched_at=fetched_at,
                                        expires_at=datetime.fromtimestamp(self.clock() + SNAPSHOT_TTL, timezone.utc),
                                        feed_title=title, format=format_name, observations=observations, duplicate_observations=duplicates)
                                    if len(snapshot.model_dump_json().encode("utf-8")) > MAX_SNAPSHOT_BYTES:
                                        raise RSSFault("parser_limit")
                                    await self.store.publish(snapshot, token)
                            except DiscoveryError as exc:
                                await self.store.defer(feed.feed_id, int(exc.retry_after or ("60" if exc.status == "throttled" else "5")))
                                raise
                            finally:
                                # Release only our own lease; never mask a fetch/parse error.
                                with anyio.move_on_after(2, shield=True):
                                    try:
                                        await self.store.release(feed.feed_id, token)
                                    except RSSFault:
                                        pass
                    page = snapshot.observations[offset:offset + request.limit]
                    next_offset = offset + len(page)
                    invalid = sum(isinstance(row, RSSRejectedEntry) for row in snapshot.observations)
                    incomplete = invalid or any(isinstance(row, RSSCandidate) and row.provenance.warnings for row in snapshot.observations)
                    return RSSDiscoveryResult(feed_id=feed.feed_id, feed_label=snapshot.feed_label,
                        feed_url=snapshot.feed_url, feed_revision=snapshot.feed_revision,
                        feed_title=snapshot.feed_title, format=snapshot.format,
                        status="incomplete" if incomplete else "success",
                        candidates=[row for row in page if isinstance(row, RSSCandidate)],
                        rejected_entries=[row for row in page if isinstance(row, RSSRejectedEntry)],
                        snapshot_id=snapshot.snapshot_id, fetched_at=snapshot.fetched_at, expires_at=snapshot.expires_at,
                        total_entries=len(snapshot.observations), invalid_entries=invalid,
                        duplicate_observations=snapshot.duplicate_observations, offset=offset, entries_in_page=len(page),
                        has_more=next_offset < len(snapshot.observations),
                        next_cursor=cursor_for(snapshot, app_id, next_offset, request.limit) if next_offset < len(snapshot.observations) else None)
        except RSSFault as exc:
            return exc.result(request.feed_id)
        except DiscoveryError as exc:
            return RSSDiscoveryResult(feed_id=request.feed_id, status=exc.status, error=str(exc), retry_after=exc.retry_after)
        except TimeoutError:
            return RSSDiscoveryResult(feed_id=request.feed_id, status="timeout", error="RSS discovery timed out.")
