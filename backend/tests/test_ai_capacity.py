"""Offline inference ownership and verified disposable Redis quota contracts."""

import asyncio
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit
from uuid import uuid4

import anyio
import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import ConnectionError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1 import ai
from app.core.auth import AuthenticatedApplication, get_current_application
from app.core.config import Settings, settings
from app.core.database import get_db
from app.core.models import ApplicationDB, APIKeyDB, AuditLogDB, CoreBase
from app.factory import create_app
from app.services import ai_admission as module, rate_limiter
from app.services.ai_admission import AIAdmissionError, RedisAIAdmission, run_admitted_inference
from app.services.api_key_service import generate_api_key
from app.services.rate_limiter import AI_GENERATION_LIMIT, GENERAL_API_LIMIT

PAYLOAD = {"system_prompt": "System", "user_prompt": "Article", "response_schema": {"type": "object"}}
RESULT = {"model": "actual-provider-model", "message": {"content": '{"classification":"relevant"}'}}


@pytest_asyncio.fixture
async def redis(monkeypatch):
    url = os.getenv("ARGUS_AI_TEST_REDIS_URL")
    expected = os.getenv("ARGUS_AI_TEST_REDIS_RUN_ID")
    if not url or not expected:
        pytest.skip("Requires verified disposable nonpersistent AI test Redis")
    parsed = urlsplit(url)
    assert parsed.hostname == "127.0.0.1" and parsed.port not in {None, 6379} and parsed.path == "/0"
    client = Redis.from_url(url, decode_responses=True)
    assert (await client.info("server"))["run_id"] == expected
    assert (await client.config_get("save"))["save"] == ""
    assert (await client.config_get("appendonly"))["appendonly"] == "no"
    assert not await client.exists(module.OWNERS_KEY, module.POLICY_KEY)
    monkeypatch.setattr(ai, "redis_client", client)
    monkeypatch.setattr(rate_limiter, "redis_client", client)
    before = set(await client.keys("argus:rate_limit:*"))
    try:
        yield client
    finally:
        assert (await client.info("server"))["run_id"] == expected
        await client.delete(module.OWNERS_KEY, module.POLICY_KEY)
        keys = set(await client.keys("argus:rate_limit:*")) - before
        if keys:
            await client.delete(*keys)
        assert not await client.exists(module.OWNERS_KEY, module.POLICY_KEY)
        await client.aclose()


def gate(redis, *, capacity=1):
    return RedisAIAdmission(redis, capacity=capacity, lease_seconds=210)


def fake_auth(application_id=None, scopes=None):
    return AuthenticatedApplication(
        application=SimpleNamespace(id=application_id or str(uuid4()), slug="global-chess"),
        api_key=SimpleNamespace(id=str(uuid4()), scopes=scopes if scopes is not None else ["ai:generate"]),
    )


def endpoint_app(auth):
    app = create_app()
    app.include_router(ai.router, prefix="/api/v1")
    app.dependency_overrides[get_current_application] = lambda: auth
    return app


def test_default_capacity_configuration():
    config = Settings(_env_file=None)
    assert config.ai_application_quota_overrides == {}
    assert config.ai_max_concurrent_inferences == 1
    assert config.ai_inference_lease_seconds == 210
    assert AI_GENERATION_LIMIT.requests == 10 and GENERAL_API_LIMIT.requests == 120


@pytest.mark.parametrize("value", [
    '[]', '{"global-chess":60}', {str(uuid4()): 61}, {str(uuid4()): 0},
    {str(uuid4()): True}, {str(uuid4()): "60"}, {str(uuid4()).upper(): 60},
    '{"11111111-1111-4111-8111-111111111111":10,"11111111-1111-4111-8111-111111111111":60}',
])
def test_malformed_or_ambiguous_quotas_fail_closed(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ai_application_quota_overrides=value)


@pytest.mark.parametrize("field,value", [
    ("ai_max_concurrent_inferences", 0), ("ai_max_concurrent_inferences", 9),
    ("ai_max_concurrent_inferences", True), ("ai_max_concurrent_inferences", 1.5),
    ("ai_inference_lease_seconds", 199), ("ai_inference_lease_seconds", 301),
])
def test_invalid_capacity_configuration(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_operator_environment_configuration(monkeypatch):
    identifier = str(uuid4())
    monkeypatch.setenv("AI_APPLICATION_QUOTA_OVERRIDES", json.dumps({identifier: 60}))
    monkeypatch.setenv("AI_MAX_CONCURRENT_INFERENCES", "2")
    config = Settings(_env_file=None)
    assert config.ai_application_quota_overrides == {identifier: 60}
    assert config.ai_max_concurrent_inferences == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("overridden", [False, True])
async def test_quota_is_shared_by_application_not_keys_or_caller_claims(redis, monkeypatch, overridden):
    identifier = str(uuid4())
    other = str(uuid4())
    monkeypatch.setattr(settings, "ai_application_quota_overrides", {identifier if overridden else other: 60})
    auth = fake_auth(identifier)
    app = endpoint_app(auth)
    backend = AsyncMock(return_value=RESULT)
    monkeypatch.setattr(ai.ollama_client, "generate_structured", backend)
    quota = 60 if overridden else 10
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as http:
        for _ in range(quota):
            auth.api_key.id = str(uuid4())
            response = await http.post("/api/v1/ai/generate", json={**PAYLOAD, "application_id": other, "quota": 60},
                                       headers={"X-Application-ID": other, "X-AI-Quota": "60"})
            assert response.status_code == 200
            assert response.json() == {"status": "success", "model": RESULT["model"], "result": RESULT["message"]["content"]}
        response = await http.post("/api/v1/ai/generate", json=PAYLOAD)
    assert response.status_code == 429 and 1 <= int(response.headers["Retry-After"]) <= 60
    assert backend.await_count == quota
    assert not await redis.zcard(module.OWNERS_KEY)


@pytest.mark.asyncio
async def test_general_quota_still_precedes_ai_quota(redis, monkeypatch):
    auth = fake_auth()
    now = time.time() * 1000
    key = f"argus:rate_limit:application:{auth.application.id}:general"
    await redis.zadd(key, {str(i): now for i in range(120)})
    app = endpoint_app(auth)
    backend = AsyncMock(return_value=RESULT)
    monkeypatch.setattr(ai.ollama_client, "generate_structured", backend)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as http:
        response = await http.post("/api/v1/ai/generate", json=PAYLOAD)
    assert response.status_code == 429 and response.headers["X-RateLimit-Limit"] == "120"
    backend.assert_not_called()


@pytest.mark.asyncio
async def test_atomic_capacity_across_independent_workers(redis):
    results = await asyncio.gather(*(gate(redis, capacity=2).acquire(f"owner-{i}") for i in range(12)), return_exceptions=True)
    assert sum(result is None for result in results) == 2
    assert all(result is None or isinstance(result, AIAdmissionError) for result in results)
    assert await redis.zcard(module.OWNERS_KEY) == 2
    assert 0 < await redis.pttl(module.OWNERS_KEY) <= 210000


@pytest.mark.asyncio
async def test_inconsistent_worker_capacity_fails_closed(redis):
    await gate(redis).acquire("owner")
    with pytest.raises(AIAdmissionError, match="unavailable"):
        await gate(redis, capacity=2).acquire("other")
    assert await redis.zcard(module.OWNERS_KEY) == 1


@pytest.mark.asyncio
async def test_expired_owner_cannot_release_new_owner(redis):
    first = gate(redis)
    await first.acquire("old")
    await redis.zadd(module.OWNERS_KEY, {"old": 0})
    await gate(redis).acquire("new")
    await first.release("old")
    assert await redis.zrange(module.OWNERS_KEY, 0, -1) == ["new"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ConnectionError("private details"), TimeoutError(), [0, "bad"], [3, 0]])
async def test_admission_redis_failure_is_sanitized(failure):
    client = SimpleNamespace(eval=AsyncMock(**({"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure})))
    with pytest.raises(AIAdmissionError) as error:
        await gate(client).acquire("owner")
    assert str(error.value) == "AI inference admission unavailable." and error.value.uncertain


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "http_error", "provider_timeout", "deadline", "malformed"])
async def test_endpoint_cleanup_preserves_contracts(redis, monkeypatch, outcome):
    backend = AsyncMock(return_value=RESULT)
    if outcome == "http_error": backend.side_effect = httpx.ConnectError("private provider detail")
    elif outcome == "provider_timeout": backend.side_effect = httpx.ReadTimeout("private provider detail")
    elif outcome == "deadline":
        async def timeout(**kwargs): raise TimeoutError()
        backend.side_effect = timeout
    elif outcome == "malformed": backend.return_value = {"private": "invalid"}
    monkeypatch.setattr(ai.ollama_client, "generate_structured", backend)
    app = endpoint_app(fake_auth())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as http:
        response = await http.post("/api/v1/ai/generate", json=PAYLOAD)
    assert response.status_code == {"success": 200, "http_error": 502, "provider_timeout": 504, "deadline": 504, "malformed": 502}[outcome]
    assert not await redis.zcard(module.OWNERS_KEY)
    assert "private" not in response.text


@pytest.mark.asyncio
async def test_busy_endpoint_does_not_call_provider(redis, monkeypatch):
    await gate(redis).acquire("other-worker")
    backend = AsyncMock()
    monkeypatch.setattr(ai.ollama_client, "generate_structured", backend)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=endpoint_app(fake_auth())), base_url="http://offline") as http:
        response = await http.post("/api/v1/ai/generate", json=PAYLOAD)
    assert response.status_code == 503 and 1 <= int(response.headers["Retry-After"]) <= 5
    assert response.json() == {"detail": "AI inference capacity is busy."}
    assert await redis.zrange(module.OWNERS_KEY, 0, -1) == ["other-worker"]
    backend.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancellation", ["native", "repeated_native", "anyio"])
async def test_active_work_owns_slot_until_finished(redis, cancellation):
    started, release, finished, cancelled = (asyncio.Event() for _ in range(4))
    async def operation():
        started.set()
        try: await release.wait()
        finally: finished.set()
        return RESULT
    async def owner():
        try:
            return await run_admitted_inference(gate(redis), operation, timeout=2)
        except asyncio.CancelledError:
            assert finished.is_set()
            cancelled.set()
            raise
    if cancellation == "anyio":
        async with anyio.create_task_group() as group:
            group.start_soon(owner)
            await started.wait()
            group.cancel_scope.cancel()
            with anyio.CancelScope(shield=True):
                assert await redis.zcard(module.OWNERS_KEY) == 1
                release.set()
        assert cancelled.is_set()
    else:
        task = asyncio.create_task(owner())
        await started.wait()
        task.cancel("original cancellation")
        # Redis round trip yields scheduling and proves the live external owner.
        assert await redis.zcard(module.OWNERS_KEY) == 1
        if cancellation == "repeated_native":
            task.cancel("second cancellation")
            assert await redis.zcard(module.OWNERS_KEY) == 1
        assert not task.done() and not finished.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError, match="original cancellation"):
            await task
    assert finished.is_set() and cancelled.is_set()
    assert not await redis.zcard(module.OWNERS_KEY)


@pytest.mark.asyncio
async def test_cancellation_during_admission_never_dispatches_and_cleans_lost_reply(redis):
    entered, release = asyncio.Event(), asyncio.Event()
    admission = gate(redis)
    original = admission.acquire
    async def acquire(token):
        await original(token)
        entered.set()
        await release.wait()
    admission.acquire = acquire
    operation = AsyncMock()
    task = asyncio.create_task(run_admitted_inference(admission, operation, timeout=2))
    await entered.wait()
    task.cancel()
    assert await redis.zcard(module.OWNERS_KEY) == 1
    release.set()
    with pytest.raises(asyncio.CancelledError): await task
    operation.assert_not_called()
    assert not await redis.zcard(module.OWNERS_KEY)


@pytest.mark.asyncio
async def test_total_deadline_joins_operation_cleanup_before_slot_release(redis, monkeypatch):
    started, finished = asyncio.Event(), asyncio.Event()
    admission = gate(redis)
    original = admission.release
    async def release(token):
        assert finished.is_set()
        await original(token)
    admission.release = release
    async def operation():
        started.set()
        try: await asyncio.Event().wait()
        finally: finished.set()
    # Arm the real operation deadline only after confirmed startup. No worker
    # scheduling assumption or short sleep is needed.
    deadline = asyncio.Timeout(None)
    monkeypatch.setattr(module.asyncio, "timeout", lambda seconds: deadline)
    task = asyncio.create_task(run_admitted_inference(admission, operation, timeout=2))
    await started.wait()
    deadline.reschedule(asyncio.get_running_loop().time())
    with pytest.raises(TimeoutError):
        await task
    assert started.is_set() and finished.is_set()
    assert not await redis.zcard(module.OWNERS_KEY)


@pytest.mark.asyncio
async def test_authentication_and_capacity_failures_are_audited(redis, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    CoreBase.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    key = generate_api_key(environment="test")
    with sessions() as db:
        application = ApplicationDB(name="Offline AI", slug="offline-ai")
        db.add(application); db.flush()
        credential = APIKeyDB(application_id=application.id, name="Offline", key_prefix=key.key_prefix,
                              key_hash=key.key_hash, scopes=["ai:generate"])
        db.add(credential); db.commit()
        identifiers = application.id, credential.id
    app = create_app(audit_session_factory=sessions)
    app.include_router(ai.router, prefix="/api/v1")
    def db_session():
        with sessions() as db: yield db
    app.dependency_overrides[get_db] = db_session
    backend = AsyncMock(return_value=RESULT)
    monkeypatch.setattr(ai.ollama_client, "generate_structured", backend)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as http:
            assert (await http.post("/api/v1/ai/generate", json=PAYLOAD)).status_code == 401
            headers = {"X-API-Key": key.plaintext_key}
            assert (await http.post("/api/v1/ai/generate", json=PAYLOAD, headers=headers)).status_code == 200
            quota_key = f"argus:rate_limit:application:{identifiers[0]}:ai_generation"
            await redis.zadd(quota_key, {str(i): time.time()*1000 for i in range(10)})
            assert (await http.post("/api/v1/ai/generate", json=PAYLOAD, headers=headers)).status_code == 429
            await redis.delete(quota_key)
            await gate(redis).acquire("busy")
            assert (await http.post("/api/v1/ai/generate", json=PAYLOAD, headers=headers)).status_code == 503
            with sessions() as db:
                db.get(APIKeyDB, identifiers[1]).scopes = []
                db.commit()
            assert (await http.post("/api/v1/ai/generate", json=PAYLOAD, headers=headers)).status_code == 403
        with sessions() as db:
            rows = list(db.scalars(select(AuditLogDB).order_by(AuditLogDB.created_at)))
            assert [row.status_code for row in rows] == [401, 200, 429, 503, 403]
            assert rows[0].application_id is None
            assert all((row.application_id, row.api_key_id) == identifiers for row in rows[1:])
            assert key.plaintext_key not in str([vars(row) for row in rows])
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_shutdown_cancelling_both_request_and_owned_task_joins_provider_cleanup(redis):
    started, cleanup_started, finish_cleanup, finished = (asyncio.Event() for _ in range(4))
    worker = []
    async def operation():
        worker.append(asyncio.current_task())
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await finish_cleanup.wait()
            finished.set()
    task = asyncio.create_task(run_admitted_inference(gate(redis), operation, timeout=2))
    await started.wait()
    # Graceful loop shutdown can cancel both tasks, not just the request.
    task.cancel()
    worker[0].cancel()
    await cleanup_started.wait()
    assert not finished.is_set() and await redis.zcard(module.OWNERS_KEY) == 1
    finish_cleanup.set()
    with pytest.raises(asyncio.CancelledError): await task
    assert finished.is_set() and not await redis.zcard(module.OWNERS_KEY)


@pytest.mark.asyncio
async def test_cancellation_after_provider_result_joins_exactly_one_release(redis):
    cleanup_started, release_cleanup = asyncio.Event(), asyncio.Event()
    admission = gate(redis)
    original = admission.release
    async def cleanup(token):
        cleanup_started.set()
        await release_cleanup.wait()
        await original(token)
    admission.release = AsyncMock(side_effect=cleanup)
    task = asyncio.create_task(run_admitted_inference(admission, AsyncMock(return_value=RESULT), timeout=2))
    await cleanup_started.wait()
    task.cancel()
    assert await redis.zcard(module.OWNERS_KEY) == 1
    release_cleanup.set()
    with pytest.raises(asyncio.CancelledError): await task
    admission.release.assert_awaited_once()
    assert not await redis.zcard(module.OWNERS_KEY)


@pytest.mark.asyncio
async def test_redis_outage_endpoint_fails_closed_without_provider_or_secret_leak(monkeypatch):
    client = SimpleNamespace(eval=AsyncMock(side_effect=ConnectionError("secret-test-detail")))
    app = endpoint_app(fake_auth())
    app.dependency_overrides[ai.require_ai_rate_limit] = lambda: None
    from app.core.rate_limit import require_rate_limit
    app.dependency_overrides[require_rate_limit(bucket="general", rate_limit=GENERAL_API_LIMIT)] = lambda: None
    app.dependency_overrides[ai.get_ai_admission] = lambda: gate(client)
    backend = AsyncMock()
    monkeypatch.setattr(ai.ollama_client, "generate_structured", backend)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as http:
        response = await http.post("/api/v1/ai/generate", json=PAYLOAD)
    assert response.status_code == 503 and response.headers["Retry-After"] == "5"
    assert "secret-test-detail" not in response.text
    backend.assert_not_called()


@pytest.mark.asyncio
async def test_lost_acquire_reply_cleans_only_its_own_token(redis):
    admission = gate(redis)
    original = admission.acquire
    async def acquire(token):
        await original(token)
        raise AIAdmissionError("AI inference admission unavailable.", uncertain=True)
    admission.acquire = acquire
    admission.release = AsyncMock(wraps=admission.release)
    operation = AsyncMock()
    with pytest.raises(AIAdmissionError):
        await run_admitted_inference(admission, operation, timeout=2)
    operation.assert_not_called()
    admission.release.assert_awaited_once()
    assert not await redis.zcard(module.OWNERS_KEY)


@pytest.mark.asyncio
async def test_release_outage_keeps_lease_for_expiration_and_original_response(redis, caplog):
    admission = gate(redis)
    admission.release = AsyncMock(side_effect=ConnectionError("secret-cleanup-detail"))
    assert await run_admitted_inference(admission, AsyncMock(return_value=RESULT), timeout=2) == RESULT
    admission.release.assert_awaited_once()
    assert await redis.zcard(module.OWNERS_KEY) == 1
    assert 0 < await redis.pttl(module.OWNERS_KEY) <= 210000
    assert "secret-cleanup-detail" not in caplog.text
