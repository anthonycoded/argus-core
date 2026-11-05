import uuid
from redis.asyncio import Redis
import pytest
import pytest_asyncio

from app.services.rate_limiter import (
    RateLimit,
    enforce_rate_limit,
)
from app.core.redis import redis_client
from app.core.config import settings
import app.services.rate_limiter as rate_limiter

@pytest_asyncio.fixture
async def redis_test_client(monkeypatch):
    client = Redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )

    monkeypatch.setattr(rate_limiter, "redis_client", client)

    yield client

    await client.aclose()

@pytest.mark.asyncio
async def test_request_allowed_under_limit(redis_test_client):
    application_id = str(uuid.uuid4())
    api_key_id = str(uuid.uuid4())

    decision = await enforce_rate_limit(
        application_id=application_id,
        api_key_id=api_key_id,
        bucket="test",
        rate_limit=RateLimit(requests=2, window_seconds=60),
    )

    assert decision.allowed is True
    assert decision.remaining == 1

    await redis_test_client.delete(
        f"argus:rate_limit:application:{application_id}:test",
        f"argus:rate_limit:key:{api_key_id}:test",
    )


@pytest.mark.asyncio
async def test_request_blocked_at_limit(redis_test_client):
    application_id = str(uuid.uuid4())
    api_key_id = str(uuid.uuid4())

    first = await enforce_rate_limit(
        application_id=application_id,
        api_key_id=api_key_id,
        bucket="test",
        rate_limit=RateLimit(requests=1, window_seconds=60),
    )

    second = await enforce_rate_limit(
        application_id=application_id,
        api_key_id=api_key_id,
        bucket="test",
        rate_limit=RateLimit(requests=1, window_seconds=60),
    )

    assert first.allowed is True
    assert second.allowed is False
    assert second.retry_after > 0

    await redis_test_client.delete(
        f"argus:rate_limit:application:{application_id}:test",
        f"argus:rate_limit:key:{api_key_id}:test",
    )


@pytest.mark.asyncio
async def test_application_limit_is_shared_across_api_keys(redis_test_client):
    application_id = str(uuid.uuid4())

    first = await enforce_rate_limit(
        application_id=application_id,
        api_key_id=str(uuid.uuid4()),
        bucket="test",
        rate_limit=RateLimit(requests=1, window_seconds=60),
    )

    second = await enforce_rate_limit(
        application_id=application_id,
        api_key_id=str(uuid.uuid4()),
        bucket="test",
        rate_limit=RateLimit(requests=1, window_seconds=60),
    )

    assert first.allowed is True
    assert second.allowed is False

    await redis_test_client.delete(
        f"argus:rate_limit:application:{application_id}:test"
    )


@pytest.mark.asyncio
async def test_different_buckets_have_independent_limits(redis_test_client):
    application_id = str(uuid.uuid4())
    api_key_id = str(uuid.uuid4())

    first = await enforce_rate_limit(
        application_id=application_id,
        api_key_id=api_key_id,
        bucket="articles",
        rate_limit=RateLimit(requests=1, window_seconds=60),
    )

    second = await enforce_rate_limit(
        application_id=application_id,
        api_key_id=api_key_id,
        bucket="ai",
        rate_limit=RateLimit(requests=1, window_seconds=60),
    )

    assert first.allowed is True
    assert second.allowed is True

    await redis_test_client.delete(
        f"argus:rate_limit:application:{application_id}:articles",
        f"argus:rate_limit:key:{api_key_id}:articles",
        f"argus:rate_limit:application:{application_id}:ai",
        f"argus:rate_limit:key:{api_key_id}:ai",
    )