"""Fail-fast, cross-worker AI admission and cancellation-owned HTTP execution."""

import asyncio
from collections.abc import Awaitable, Callable
import logging
import secrets
from typing import TypeVar

import anyio
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)
T = TypeVar("T")

# One backend-wide namespace, independent of application, key, or worker.
OWNERS_KEY = "argus:ai:{inference}:owners:v1"
POLICY_KEY = "argus:ai:{inference}:policy:v1"
REDIS_TIMEOUT = 2.0

ACQUIRE = """
local t = redis.call('TIME')
local now = tonumber(t[1])*1000 + math.floor(tonumber(t[2])/1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local count = redis.call('ZCARD', KEYS[1])
if count == 0 then
  redis.call('HSET', KEYS[2], 'capacity', ARGV[2], 'lease', ARGV[3])
end
if redis.call('HGET', KEYS[2], 'capacity') ~= ARGV[2]
    or redis.call('HGET', KEYS[2], 'lease') ~= ARGV[3] then return {2, 5} end
if count >= tonumber(ARGV[2]) then
  local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return {1, math.max(1, math.min(5, math.ceil((tonumber(first[2])-now)/1000)))}
end
redis.call('ZADD', KEYS[1], now+tonumber(ARGV[3]), ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[3])
redis.call('PEXPIRE', KEYS[2], ARGV[3])
return {0, 0}
"""

RELEASE = """
return redis.call('ZREM', KEYS[1], ARGV[1])
"""


class AIAdmissionError(Exception):
    def __init__(self, detail: str, retry_after: int = 5, *, uncertain: bool = False):
        super().__init__(detail)
        self.retry_after = retry_after
        self.uncertain = uncertain


class RedisAIAdmission:
    def __init__(self, client, *, capacity: int, lease_seconds: int):
        self.client = client
        self.capacity = capacity
        self.lease_seconds = lease_seconds

    async def acquire(self, token: str) -> None:
        try:
            with anyio.fail_after(REDIS_TIMEOUT):
                result = await self.client.eval(
                    ACQUIRE, 2, OWNERS_KEY, POLICY_KEY,
                    token, self.capacity, self.lease_seconds * 1000,
                )
            if (not isinstance(result, (list, tuple)) or len(result) != 2
                    or any(type(item) is not int for item in result)):
                raise ValueError
            code, retry = result
            if code == 0 and retry == 0:
                return
            if code in {1, 2} and 1 <= retry <= 5:
                raise AIAdmissionError(
                    "AI inference capacity is busy." if code == 1 else "AI inference capacity unavailable.",
                    retry,
                )
            raise ValueError
        except (RedisError, TimeoutError, ValueError, TypeError):
            # A lost Redis reply may still have installed this owner's token.
            raise AIAdmissionError("AI inference admission unavailable.", uncertain=True) from None

    async def release(self, token: str) -> None:
        with anyio.fail_after(REDIS_TIMEOUT):
            result = await self.client.eval(RELEASE, 1, OWNERS_KEY, token)
        if type(result) is not int or result not in {0, 1}:
            raise ValueError("Invalid admission release response")


async def run_admitted_inference(
    admission: RedisAIAdmission, operation: Callable[[], Awaitable[T]], *, timeout: float,
) -> T:
    """Join owned work before propagating native/AnyIO caller cancellation.

    A cancelled caller cannot release a slot underneath an HTTP operation. Work
    that has started finishes within its own total deadline; it is never retried.
    At most one owned task exists per request; Redis admits only bounded work.
    """
    cancelled = asyncio.Event()

    async def owned():
        token = secrets.token_hex(32)
        release_needed = True
        with anyio.CancelScope(shield=True):
            try:
                try:
                    await admission.acquire(token)
                except AIAdmissionError as exc:
                    release_needed = exc.uncertain
                    raise
                await anyio.lowlevel.checkpoint()
                if cancelled.is_set():
                    return None  # Caller cancelled during admission: never dispatch.
                async with asyncio.timeout(timeout):
                    return await operation()
            finally:
                if release_needed:
                    # One attempt, joined by the owner. TTL recovers Redis failures.
                    try:
                        with anyio.fail_after(REDIS_TIMEOUT):
                            await admission.release(token)
                    except (RedisError, TimeoutError, ValueError, TypeError):
                        logger.warning("AI inference capacity cleanup unavailable; lease expiry will recover capacity.")

    job = asyncio.create_task(owned())
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        cancelled.set()
        with anyio.CancelScope(shield=True):
            while not job.done():
                try:
                    await asyncio.shield(job)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break  # Retrieve the exception below; preserve caller cancellation.
        if not job.cancelled():
            job.exception()
        raise
