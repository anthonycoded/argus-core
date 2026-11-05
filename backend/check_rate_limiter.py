import asyncio
from uuid import uuid4

from app.services.rate_limiter import RateLimit, enforce_rate_limit


async def main():
    app_id = str(uuid4())
    key_id = str(uuid4())

    limit = RateLimit(requests=1, window_seconds=60)

    first = await enforce_rate_limit(
        application_id=app_id,
        api_key_id=key_id,
        bucket="verification",
        rate_limit=limit,
    )

    second = await enforce_rate_limit(
        application_id=app_id,
        api_key_id=key_id,
        bucket="verification",
        rate_limit=limit,
    )

    print("First request:", first)
    print("Second request:", second)


if __name__ == "__main__":
    asyncio.run(main())