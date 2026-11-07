"""Core authentication/auditing with memory SQLite and a mocked GDELT transport."""

import json
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest
from sqlalchemy import create_engine,event,func,select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.v1.discovery import get_gdelt_provider
from app.api.v1.router import api_router
from app.core.database import Base,get_db
from app.core.models import APIKeyDB,ApplicationDB,AuditLogDB,CoreBase,SourceRecordDB
from app.core import rate_limit
from app.factory import create_app
from app.services.api_key_service import generate_api_key
from app.services.discovery.gdelt import GDELTProvider
from app.services.discovery.gdelt_fetch import SecureGDELTFetcher
from app.services.rate_limiter import RateLimitDecision


class Body(httpx.AsyncByteStream):
    def __init__(self,data):self.data=data
    async def __aiter__(self):yield self.data
    async def aclose(self):pass


@pytest.fixture
def context(monkeypatch):
    from app import models
    monkeypatch.setattr(httpcore.AsyncConnectionPool,"handle_async_request",AsyncMock(side_effect=AssertionError("Live HTTP")))
    engine=create_engine("sqlite://",connect_args={"check_same_thread":False},poolclass=StaticPool)
    CoreBase.metadata.create_all(engine);Base.metadata.create_all(engine);sessions=sessionmaker(bind=engine)
    keys=[]
    with sessions() as db:
        application=ApplicationDB(name="GDELT tests",slug="gdelt-tests");db.add(application);db.flush()
        for scopes in (["discovery:read"],["sources:read"]):
            key=generate_api_key(environment="test");keys.append(key.plaintext_key)
            db.add(APIKeyDB(application_id=application.id,name="Offline",key_prefix=key.key_prefix,key_hash=key.key_hash,scopes=scopes))
        db.commit()
    writes=[]
    def guard(conn,cursor,statement,parameters,context,executemany):
        sql=statement.strip().lower()
        if sql.startswith(("insert","update","delete","create","drop","alter")):
            assert sql.startswith(("update core_api_keys set last_used_at=","insert into core_audit_logs"))
            writes.append(sql)
    event.listen(engine,"before_cursor_execute",guard)
    state={"status":200,"body":b'{"articles":[]}',"headers":{}};outbound=[]
    def respond(request):
        outbound.append(request)
        assert "x-api-key" not in request.headers and "authorization" not in request.headers
        return httpx.Response(state["status"],headers={"content-type":"application/json",**state["headers"]},stream=Body(state["body"]))
    admission=AsyncMock();admission.acquire.return_value="owned-token";admission.finish.return_value="60"
    provider=GDELTProvider(admission,SecureGDELTFetcher(httpx.MockTransport(respond)),enabled=True)
    app=create_app(routers=(api_router,),audit_session_factory=sessions)
    def session():
        with sessions() as db:yield db
    app.dependency_overrides[get_db]=session
    app.dependency_overrides[get_gdelt_provider]=lambda:provider
    limiter=AsyncMock(return_value=RateLimitDecision(True,10,9,0))
    monkeypatch.setattr(rate_limit,"enforce_rate_limit",limiter)
    yield dict(app=app,keys=keys,provider=provider,admission=admission,state=state,outbound=outbound,
               limiter=limiter,sessions=sessions,writes=writes)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(SourceRecordDB))==0
        for table in Base.metadata.sorted_tables:assert db.scalar(select(func.count()).select_from(table))==0
    engine.dispose()


async def post(context,key=0,payload=None):
    headers={} if key is None else {"X-API-Key":context["keys"][key] if isinstance(key,int) else key}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=context["app"]),base_url="http://core.test") as client:
        return await client.post("/api/v1/discovery",headers=headers,json=payload or {"provider":"gdelt","query":"Ukraine NATO","limit":10})


@pytest.mark.asyncio
async def test_authenticated_empty_success_quotas_audit_and_no_persistence(context):
    response=await post(context)
    assert response.status_code==200 and response.json()["status"]=="success" and response.json()["candidates"]==[]
    assert [call.kwargs["bucket"] for call in context["limiter"].call_args_list]==["general","discovery"]
    assert len(context["outbound"])==1
    with context["sessions"]() as db:
        log,=db.scalars(select(AuditLogDB)).all()
        assert log.application_id and log.api_key_id and log.status_code==200 and log.path=="/discovery"


@pytest.mark.asyncio
@pytest.mark.parametrize("key,status",[(None,401),("invalid",401),(1,403)])
async def test_auth_and_scope_cannot_be_bypassed(context,key,status):
    assert (await post(context,key)).status_code==status
    context["admission"].acquire.assert_not_awaited();assert not context["outbound"]
    with context["sessions"]() as db:assert db.scalar(select(AuditLogDB.status_code))==status


@pytest.mark.asyncio
@pytest.mark.parametrize("bucket",["general","discovery"])
async def test_quota_denial_before_admission(context,bucket):
    async def limit(**kwargs):return RateLimitDecision(kwargs["bucket"]!=bucket,10,0,17)
    context["limiter"].side_effect=limit
    response=await post(context)
    assert response.status_code==429 and response.headers["Retry-After"]=="17"
    context["admission"].acquire.assert_not_awaited();assert not context["outbound"]
    with context["sessions"]() as db:assert db.scalar(select(AuditLogDB.status_code))==429


@pytest.mark.asyncio
async def test_default_disabled_uses_no_provider_state(context,monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings,"gdelt_enabled",False)
    context["app"].dependency_overrides.pop(get_gdelt_provider)
    response=await post(context)
    assert response.status_code==503 and response.json()["status"]=="disabled" and not context["outbound"]
    context["admission"].acquire.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,status",[("malformed",502),("throttle",429),("upstream",502),("unsafe",502)])
async def test_provider_errors_sanitized_and_audited(context,mode,status):
    if mode=="malformed":context["state"]["body"]=b'private upstream diagnostic'
    elif mode=="throttle":context["state"].update(status=429,headers={"retry-after":"60"})
    elif mode=="upstream":context["state"]["status"]=503
    else:context["state"].update(status=302,headers={"location":"http://169.254.169.254/"})
    response=await post(context)
    assert response.status_code==status and "private" not in response.text
    assert all(key not in response.text for key in context["keys"])
    assert len(context["outbound"])==1
    if mode=="throttle":assert response.headers["Retry-After"]=="60"
    with context["sessions"]() as db:assert db.scalar(select(AuditLogDB.status_code))==status


@pytest.mark.asyncio
@pytest.mark.parametrize("payload",[{"provider":"gdelt","query":"domain:example.com"},
    {"provider":"gdelt","query":"Ukraine","url":"http://localhost"},
    {"provider":"gdelt","query":"Ukraine","headers":{"X-API-Key":"not-forwarded"}}])
async def test_invalid_input_never_fetches(context,payload):
    assert (await post(context,payload=payload)).status_code==422
    assert not context["outbound"]
