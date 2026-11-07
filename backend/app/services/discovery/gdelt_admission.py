"""Two fixed Redis keys coordinate every worker/application using this egress."""

import secrets
from typing import Protocol

import anyio
from redis.exceptions import RedisError

from app.services.discovery.gdelt_contracts import GDELTFault, RetryPolicy

STATE_KEY = "argus:discovery:gdelt:v1:state"
LEASE_KEY = "argus:discovery:gdelt:v1:lease"
START_GAP_MS = 6000
LEASE_MS = 40000

ACQUIRE = """
local t = redis.call('TIME')
local now = tonumber(t[1])*1000 + math.floor(tonumber(t[2])/1000)
if redis.call('HGET', KEYS[1], 'hold') == '1' then return {3, 0} end
local cooldown = tonumber(redis.call('HGET', KEYS[1], 'cooldown') or '0')
local next_start = tonumber(redis.call('HGET', KEYS[1], 'next') or '0')
local wait = math.max(cooldown, next_start) - now
if wait > 0 then return {1, math.max(1, math.ceil(wait/1000))} end
local ttl = redis.call('PTTL', KEYS[2])
if ttl ~= -2 then return {2, math.max(1, math.ceil(math.max(ttl,1)/1000))} end
redis.call('SET', KEYS[2], ARGV[1], 'PX', ARGV[3])
redis.call('HSET', KEYS[1], 'next', now+tonumber(ARGV[2]))
return {0, 0}
"""

FINISH = """
local t = redis.call('TIME')
local now = tonumber(t[1])*1000 + math.floor(tonumber(t[2])/1000)
local cooldown = tonumber(redis.call('HGET', KEYS[1], 'cooldown') or '0')
local until_ms = math.max(cooldown, now+tonumber(ARGV[2])*1000, tonumber(ARGV[3]))
redis.call('HSET', KEYS[1], 'cooldown', until_ms)
if ARGV[4] == '1' then redis.call('HSET', KEYS[1], 'hold', '1') end
local owned = 0
if redis.call('GET', KEYS[2]) == ARGV[1] then
  redis.call('DEL', KEYS[2])
  owned = 1
end
local next_start = tonumber(redis.call('HGET', KEYS[1], 'next') or '0')
return {owned, math.max(1, math.ceil((math.max(until_ms,next_start)-now)/1000))}
"""


class GDELTAdmission(Protocol):
    async def acquire(self) -> str: ...
    async def finish(self, token: str, policy: RetryPolicy) -> str: ...


class RedisGDELTAdmission:
    def __init__(self, client):
        self.client = client

    async def _eval(self, script, *args):
        try:
            with anyio.fail_after(2):
                result = await self.client.eval(script, 2, STATE_KEY, LEASE_KEY, *args)
            if not isinstance(result, (list, tuple)) or len(result) != 2 or any(type(n) is not int for n in result):
                raise ValueError
            return result
        except (RedisError, TimeoutError, ValueError, TypeError):
            raise GDELTFault("storage_unavailable") from None

    async def acquire(self):
        token = secrets.token_hex(16)
        code, retry = await self._eval(ACQUIRE, token, START_GAP_MS, LEASE_MS)
        if code:
            if code not in {1, 2, 3}:
                raise GDELTFault("storage_unavailable")
            raise GDELTFault({1:"throttled", 2:"busy", 3:"paused"}[code], str(retry) if retry > 0 else None)
        return token

    async def finish(self, token, policy):
        owned, retry = await self._eval(FINISH, token, policy.delay_seconds, policy.until_ms, int(policy.hold))
        if owned != 1:
            raise GDELTFault("storage_unavailable")
        return str(retry)
