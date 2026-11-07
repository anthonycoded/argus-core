"""Application factory/settings boundaries; providers and databases are isolated."""

import ast
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from pydantic import ValidationError

from app import config
from app.clients.global_chess_ollama_client import OllamaClient
from app.global_chess_config import GlobalChessSettings
from app.global_chess_factory import create_app


@pytest.fixture
def clean_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith("GLOBAL_CHESS_") or key in {"APP_NAME","APP_VERSION","OLLAMA_BASE_URL","OLLAMA_MODEL","DATABASE_URL","ENVIRONMENT"}:
            monkeypatch.delenv(key)


def test_existing_openapi_metadata_routes_and_middleware_are_preserved():
    from app import main
    expected=json.loads((Path(__file__).parent/"fixtures/global_chess_openapi.json").read_text())
    assert main.app.openapi()==expected
    assert main.app.title=="CivicStudio AI - Trend Scout" and main.app.version=="0.1.0"
    assert main.app.user_middleware==[]
    assert main.create_app is create_app


def test_application_factory_uses_owned_settings_and_explicit_overrides(monkeypatch,clean_environment):
    own=GlobalChessSettings(_env_file=None,app_name="Owned application",app_version="6C")
    monkeypatch.setattr("app.global_chess_factory.global_chess_settings",own)
    app=create_app()
    assert (app.title,app.version)==("Owned application","6C")
    explicit=create_app(title="Explicit title",version="Explicit version")
    assert (explicit.title,explicit.version)==("Explicit title","Explicit version")
    assert not app.user_middleware


@pytest.mark.asyncio
@pytest.mark.parametrize("fails",[False,True])
async def test_factory_preserves_callback_order_and_cleanup_on_startup_failure(fails):
    calls=[]
    def initialize():
        calls.append("initialize")
        if fails:raise RuntimeError("isolated startup failure")
    async def close():calls.append("close")
    app=create_app(initialize_database=initialize,on_shutdown=close)
    if fails:
        with pytest.raises(RuntimeError,match="isolated startup failure"):
            async with app.router.lifespan_context(app):pytest.fail("Unexpected startup")
        assert calls==["initialize","close"]
    else:
        async with app.router.lifespan_context(app):calls.append("serve")
        assert calls==["initialize","serve","close"]


@pytest.mark.asyncio
async def test_factory_without_callbacks_has_no_database_side_effects(monkeypatch):
    from sqlalchemy.engine import Engine
    connect=Mock(side_effect=AssertionError("Factory connected to a database"))
    monkeypatch.setattr(Engine,"connect",connect)
    app=create_app()
    async with app.router.lifespan_context(app):pass
    connect.assert_not_called()


def test_legacy_aliases_and_owned_overrides_preserve_provider_configuration(clean_environment,monkeypatch):
    monkeypatch.setenv("APP_NAME","Core-only name")
    monkeypatch.setenv("APP_VERSION","legacy-version")
    monkeypatch.setenv("OLLAMA_BASE_URL","http://legacy-model.invalid:12400")
    monkeypatch.setenv("OLLAMA_MODEL","legacy-model")
    legacy=GlobalChessSettings(_env_file=None)
    assert legacy.app_name=="CivicStudio AI - Trend Scout"
    assert legacy.app_version=="legacy-version"
    assert legacy.ollama_base_url.get_secret_value()=="http://legacy-model.invalid:12400"
    assert legacy.ollama_model=="legacy-model"
    monkeypatch.setenv("GLOBAL_CHESS_APP_NAME","Independent application")
    monkeypatch.setenv("GLOBAL_CHESS_APP_VERSION","owned-version")
    monkeypatch.setenv("GLOBAL_CHESS_OLLAMA_BASE_URL","http://owned-model.invalid:12400")
    monkeypatch.setenv("GLOBAL_CHESS_OLLAMA_MODEL","owned-model")
    owned=GlobalChessSettings(_env_file=None)
    assert owned.app_name=="Independent application" and owned.app_version=="owned-version"
    assert owned.ollama_base_url.get_secret_value()=="http://owned-model.invalid:12400"
    assert owned.ollama_model=="owned-model"


def test_compatibility_exports_feed_order_and_scoring_defaults(clean_environment):
    settings=GlobalChessSettings(_env_file=None)
    assert settings.research_rss_feeds==settings.ingestion_rss_feeds==(("BBC World","bbc-world"),("NPR Politics","npr-politics"))
    assert list(config.FEEDS.items())==[("BBC World","https://feeds.bbci.co.uk/news/world/rss.xml"),
        ("NPR Politics","https://feeds.npr.org/1014/rss.xml")]
    assert config.KEYWORDS==settings.editorial_keywords
    assert (settings.relevance_weight,settings.freshness_weight)==(0.7,0.3)
    assert (settings.freshness_window_hours,settings.missing_date_freshness)==(72,40)
    assert isinstance(config.OLLAMA_BASE_URL,str) and isinstance(config.OLLAMA_MODEL,str)


@pytest.mark.parametrize("options",[
    dict(research_rss_feeds=(("Unknown","arbitrary"),)),
    dict(ingestion_rss_feeds=(("First","bbc-world"),("Second","bbc-world"))),
    dict(editorial_keywords={"phrase":True}),dict(editorial_keywords={"phrase":101}),
    dict(relevance_weight=0.8),dict(freshness_window_hours=0),dict(missing_date_freshness=101),
    dict(relevance_weight=float("nan")),
])
def test_invalid_application_policy_fails_without_secret_values(clean_environment,options):
    key="synthetic-private-key-not-a-real-credential"
    with pytest.raises(ValidationError) as error:
        GlobalChessSettings(_env_file=None,core_api_key=key,**options)
    assert key not in str(error.value)


def test_settings_mask_provider_and_api_secrets(clean_environment):
    marker="synthetic-provider-secret"
    url="https://synthetic:"+marker+"@model.invalid"
    settings=GlobalChessSettings(_env_file=None,ollama_base_url=url,core_api_key=marker,database_url="sqlite://")
    assert marker not in repr(settings) and marker not in settings.model_dump_json()


@pytest.mark.asyncio
async def test_legacy_classifier_client_keeps_original_ollama_wire_behavior():
    response=httpx.Response(200,json={"message":{"content":'{"classification":"relevant","rationale":"Supplied evidence"}'}},
        request=httpx.Request("POST","https://model.invalid/api/chat"))
    http=AsyncMock();http.post.return_value=response
    with patch("app.clients.global_chess_ollama_client.httpx.AsyncClient") as constructor:
        constructor.return_value.__aenter__.return_value=http
        client=OllamaClient(base_url="https://model.invalid/",model="preserved-model")
        result=await client.generate_structured(system_prompt="System",user_prompt="Article",
            response_schema={"type":"object"},timeout=60,temperature=0)
        constructor.assert_called_once_with(timeout=60)
        http.post.assert_awaited_once_with("https://model.invalid/api/chat",json={
            "model":"preserved-model","stream":False,"format":{"type":"object"},
            "messages":[{"role":"system","content":"System"},{"role":"user","content":"Article"}],
            "options":{"temperature":0},
        })
        constructor.return_value.__aexit__.assert_awaited_once()
    assert result==response.json()


@pytest.mark.asyncio
async def test_public_responses_and_logs_do_not_expose_configured_keys(monkeypatch,caplog):
    from app import main
    marker="synthetic-private-core-key"
    monkeypatch.setattr("app.global_chess_config.global_chess_settings.core_api_key",marker)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url="http://offline.test") as client:
        health=await client.get("/health")
        missing=await client.get("/missing")
        invalid=await client.patch("/topics/not-an-integer/status",json={"status":"approved"})
        schema=await client.get("/openapi.json")
    assert health.json()=={"status":"ok","service":"trend-scout"}
    assert missing.status_code==404 and invalid.status_code==422
    assert all(marker not in response.text for response in (health,missing,invalid,schema))
    assert marker not in caplog.text


@pytest.mark.parametrize("application",["global-chess","core"])
def test_fresh_process_application_import_boundary(application):
    env=os.environ.copy()
    for key in list(env):
        if key.startswith("GLOBAL_CHESS_") or key in {"APP_NAME","APP_VERSION","ENVIRONMENT","DATABASE_URL","OLLAMA_BASE_URL","OLLAMA_MODEL"}:
            env.pop(key)
    env.update(PYTHONDONTWRITEBYTECODE="1",DATABASE_URL="sqlite://",ENVIRONMENT="test",APP_VERSION="0.1.0")
    bootstrap="""
import sys,pathlib,importlib.abc
from pydantic_settings.sources.providers.dotenv import DotEnvSettingsSource
DotEnvSettingsSource._read_env_files=lambda self:{}
sys.path.insert(0,str(pathlib.Path('backend').resolve()))
"""
    if application=="global-chess":
        env.update(GLOBAL_CHESS_DATABASE_URL="sqlite://",GLOBAL_CHESS_ENVIRONMENT="test",GLOBAL_CHESS_ALLOW_LEGACY_DATABASE_URL="false")
        program="""
class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname=='app.factory' or fullname=='app.core' or fullname.startswith('app.core.'):
            raise AssertionError('Global Chess imported Core startup/configuration')
sys.meta_path.insert(0,Boundary())
from app import main,database
from sqlalchemy import create_engine,inspect
from sqlalchemy.pool import StaticPool
import asyncio,httpx
database.engine.dispose()
owned=create_engine('sqlite://',poolclass=StaticPool,connect_args={'check_same_thread':False})
main.engine=owned
checks=[]
main.validate_global_chess_database=lambda engine: checks.append(engine)
async def run():
    async with main.app.router.lifespan_context(main.app):
        assert checks==[owned] and inspect(owned).get_table_names()==[]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://offline.test') as client:
            assert (await client.get('/health')).status_code==200
asyncio.run(run())
assert 'app.factory' not in sys.modules
assert not any(name=='app.core' or name.startswith('app.core.') for name in sys.modules)
"""
    else:
        program="""
class Boundary(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname in {'app.global_chess_factory','app.global_chess_config','app.main','app.database','app.config','app.models'}:
            raise AssertionError('Core imported Global Chess startup/configuration')
sys.meta_path.insert(0,Boundary())
from app import core_main
assert core_main.app.title=='Argus-Core'
assert 'app.global_chess_factory' not in sys.modules and 'app.global_chess_config' not in sys.modules
core_main.audit_sessions.kw['bind'].dispose()
"""
    result=subprocess.run([sys.executable,"-B","-c",bootstrap+program],env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr


def test_application_owned_modules_do_not_import_core_factory_or_settings():
    root=Path(__file__).parents[1]/"app"
    files=[root/"main.py",root/"global_chess_factory.py",root/"global_chess_config.py",root/"config.py",
        root/"clients/global_chess_ollama_client.py",root/"services/ollama_service.py",root/"services/topic_scoring.py"]
    for path in files:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node,ast.ImportFrom):assert node.module not in {"app.factory","app.core.config","app.clients.ollama_client"},str(path)
