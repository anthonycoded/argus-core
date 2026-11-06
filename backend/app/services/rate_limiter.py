from dataclasses import dataclass
import math
import secrets
import time
from dataclasses import dataclass

from app.core.redis import redis_client

@dataclass(frozen=True)
class RateLimit:
    requests: int
    window_seconds: int


GENERAL_API_LIMIT = RateLimit(
    requests=120,
    window_seconds=60,
)

SOURCE_WRITE_LIMIT = RateLimit(
    requests=60,
    window_seconds=60,
)

ARTICLE_EXTRACTION_LIMIT = RateLimit(
    requests=10,
    window_seconds=60,
)

AI_GENERATION_LIMIT = RateLimit(
    requests=10,
    window_seconds=60,
)

DISCOVERY_LIMIT = RateLimit(
    requests=10,
    window_seconds=60,
)

# Cursor pages only read existing immutable RSS snapshots; they cannot fetch.
RSS_CONTINUATION_LIMIT = RateLimit(
    requests=60,
    window_seconds=60,
)


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


SLIDING_WINDOW_SCRIPT = """
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

for _, key in ipairs(KEYS) do
    redis.call("ZREMRANGEBYSCORE", key, "-inf", now - window)

    local count = redis.call("ZCARD", key)

    if count >= limit then
        local oldest = redis.call("ZRANGE", key, 0, 0, "WITHSCORES")
        local retry_after = 1

        if oldest[2] then
            retry_after = math.max(
                1,
                math.ceil((tonumber(oldest[2]) + window - now) / 1000)
            )
        end

        return {0, 0, retry_after}
    end
end

local remaining = limit

for _, key in ipairs(KEYS) do
    redis.call("ZADD", key, now, member)
    redis.call("PEXPIRE", key, window)

    local count = redis.call("ZCARD", key)
    remaining = math.min(remaining, limit - count)
end

return {1, remaining, 0}
"""


async def enforce_rate_limit(
    *,
    application_id: str,
    api_key_id: str,
    bucket: str,
    rate_limit: RateLimit,
) -> RateLimitDecision:

    now_ms = int(time.time() * 1000)
    window_ms = rate_limit.window_seconds * 1000

    application_key = (
        f"argus:rate_limit:application:{application_id}:{bucket}"
    )

    api_key = (
        f"argus:rate_limit:key:{api_key_id}:{bucket}"
    )

    result = await redis_client.eval(
        SLIDING_WINDOW_SCRIPT,
        2,
        application_key,
        api_key,
        now_ms,
        window_ms,
        rate_limit.requests,
        secrets.token_urlsafe(16),
    )

    allowed = bool(result[0])

    return RateLimitDecision(
        allowed=allowed,
        limit=rate_limit.requests,
        remaining=int(result[1]),
        retry_after=int(result[2]),
    )
