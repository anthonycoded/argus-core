"""Global Chess database ownership, configuration and lifecycle; no persistent targets."""

import ast
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateIndex, CreateTable

from app import database as chess_db
from app import models
from app.core import database as core_db
from app.core.models import CoreBase
from app.global_chess_config import GlobalChessDatabaseConfigurationError, GlobalChessSettings

CHESS_TABLES = {"topics", "topic_analyses", "research_sources", "story_clusters", "story_cluster_members"}
CORE_TABLES = {"core_sources", "core_applications", "core_api_keys", "core_audit_logs", "core_source_catalogs", "core_source_admission_events"}


@pytest.fixture
def clean_settings_environment(monkeypatch):
    for key in list(os.environ):
        if key.startswith("GLOBAL_CHESS_") or key in {"DATABASE_URL", "ENVIRONMENT"}:
            monkeypatch.delenv(key)


def test_metadata_is_separate_and_five_table_ddl_is_unchanged():
    assert set(chess_db.Base.metadata.tables) == CHESS_TABLES
    assert set(CoreBase.metadata.tables) == CORE_TABLES
    assert chess_db.Base.metadata is not CoreBase.metadata
    assert not core_db.Base.metadata.tables  # Retained legacy class owns neither schema.
    actual = {name: {"table": str(CreateTable(table).compile(dialect=dialect())).strip(),
        "indexes": sorted(str(CreateIndex(index).compile(dialect=dialect())) for index in table.indexes)}
        for name, table in sorted(chess_db.Base.metadata.tables.items())}
    expected = json.loads((Path(__file__).parent / "fixtures/global_chess_schema.json").read_text())
    assert actual == expected
    assert all(fk.column.table.metadata is chess_db.Base.metadata
        for table in chess_db.Base.metadata.tables.values() for fk in table.foreign_keys)


def test_engines_sessions_and_dependency_functions_are_distinct():
    assert chess_db.engine is not core_db.engine
    assert chess_db.SessionLocal is not core_db.SessionLocal
    assert chess_db.get_db is not core_db.get_db
    assert chess_db.SessionLocal.kw["bind"] is chess_db.engine
    assert core_db.SessionLocal.kw["bind"] is core_db.engine
    assert chess_db.SessionLocal.kw["autoflush"] is False
    assert chess_db.SessionLocal.kw["autocommit"] is False


def test_explicit_url_wins_without_fallback_or_secret_disclosure(clean_settings_environment, caplog):
    explicit = "postgresql+psycopg://synthetic:private-fixture@example.invalid/global_chess"
    settings = GlobalChessSettings(_env_file=None, database_url=explicit,
        legacy_database_url="sqlite://", environment="production")
    assert settings.database_connection_url() == explicit
    assert explicit not in repr(settings) and "private-fixture" not in settings.model_dump_json()
    assert not caplog.text


def test_development_compatibility_preserves_exact_legacy_url_and_warns(clean_settings_environment, monkeypatch, caplog):
    legacy = "postgresql+psycopg://synthetic:private-fixture@example.invalid/existing"
    monkeypatch.setenv("DATABASE_URL", legacy)
    settings = GlobalChessSettings(_env_file=None)
    assert settings.database_connection_url() == legacy
    assert "temporary development-only" in caplog.text
    assert legacy not in caplog.text and "private-fixture" not in caplog.text
    assert legacy not in repr(settings) and "legacy_database_url" not in settings.model_dump()


@pytest.mark.parametrize("environment,legacy_environment", [("production",None),("prod",None),
    ("staging",None),("test",None),("development","production"),("development","prod"),
    ("development","staging"),("unknown",None)])
def test_missing_explicit_database_fails_outside_development(clean_settings_environment, environment, legacy_environment):
    settings = GlobalChessSettings(_env_file=None, environment=environment,
        legacy_environment=legacy_environment, legacy_database_url="sqlite://")
    with pytest.raises(GlobalChessDatabaseConfigurationError, match="GLOBAL_CHESS_DATABASE_URL is required"):
        settings.database_connection_url()


def test_both_environment_flags_are_respected(clean_settings_environment, monkeypatch):
    monkeypatch.setenv("GLOBAL_CHESS_ENVIRONMENT", "development")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    with pytest.raises(GlobalChessDatabaseConfigurationError):
        GlobalChessSettings(_env_file=None).database_connection_url()


@pytest.mark.parametrize("options", [dict(),dict(allow_legacy_database_url=False,legacy_database_url="sqlite://")])
def test_missing_database_or_disabled_compatibility_fails_clearly(clean_settings_environment, options):
    with pytest.raises(GlobalChessDatabaseConfigurationError):
        GlobalChessSettings(_env_file=None, **options).database_connection_url()


@pytest.mark.parametrize("value", ["", " ", "\t"])
def test_explicit_blank_url_never_falls_back(clean_settings_environment, value):
    with pytest.raises(ValidationError):
        GlobalChessSettings(_env_file=None,database_url=value,legacy_database_url="sqlite://")


@pytest.mark.parametrize("exception", [False, True])
def test_session_dependency_closes_its_own_session_exactly_once(monkeypatch, exception):
    session = Mock()
    core_factory = Mock(side_effect=AssertionError("Global Chess opened a Core session"))
    monkeypatch.setattr(chess_db,"SessionLocal",Mock(return_value=session))
    monkeypatch.setattr(core_db,"SessionLocal",core_factory)
    dependency = chess_db.get_db()
    assert next(dependency) is session
    if exception:
        with pytest.raises(RuntimeError):dependency.throw(RuntimeError("isolated request failure"))
    else:
        with pytest.raises(StopIteration):next(dependency)
    session.close.assert_called_once()
    core_factory.assert_not_called()


def test_dependency_rollback_preserves_committed_records(monkeypatch):
    engine=create_engine("sqlite://",poolclass=StaticPool,connect_args={"check_same_thread":False})
    chess_db.Base.metadata.create_all(engine)
    monkeypatch.setattr(chess_db,"SessionLocal",sessionmaker(bind=engine,autoflush=False,autocommit=False))
    try:
        dependency=chess_db.get_db();db=next(dependency)
        db.add(models.Topic(id=1,title="Preserved",url="https://example.com/1",source="Fixture",
            relevance_score=1,freshness_score=2,score=3,status="approved",summary="Preserved summary"))
        db.commit();dependency.close()
        dependency=chess_db.get_db();db=next(dependency)
        db.add(models.Topic(id=2,title="Rolled back",url="https://example.com/2",source="Fixture",
            relevance_score=1,freshness_score=2,score=3))
        db.flush();dependency.close()
        with chess_db.SessionLocal() as db:
            row,=db.scalars(select(models.Topic)).all()
            assert row.id==1 and row.status=="approved" and row.summary=="Preserved summary"
        assert set(inspect(engine).get_table_names()) == CHESS_TABLES
    finally:engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_fails", [False,True])
async def test_actual_chess_lifespan_disposes_only_chess_engine(monkeypatch,startup_fails):
    from app import main
    own = Mock()
    core_dispose = Mock(side_effect=AssertionError("Global Chess disposed Core"))
    monkeypatch.setattr(main,"engine",own)
    monkeypatch.setattr(core_db.engine,"dispose",core_dispose)
    create = Mock(side_effect=RuntimeError("isolated startup failure") if startup_fails else None)
    monkeypatch.setattr(chess_db.Base.metadata,"create_all",create)
    if startup_fails:
        with pytest.raises(RuntimeError,match="isolated startup failure"):
            async with main.app.router.lifespan_context(main.app):pytest.fail("Startup should fail")
    else:
        async with main.app.router.lifespan_context(main.app):pass
    create.assert_called_once_with(bind=own)
    own.dispose.assert_called_once()
    core_dispose.assert_not_called()


@pytest.mark.asyncio
async def test_core_shutdown_does_not_dispose_chess_engine(monkeypatch):
    from app import core_main
    monkeypatch.setattr(core_main,"close_redis",AsyncMock())
    monkeypatch.setattr(core_main.audit_sessions.kw["bind"],"dispose",Mock())
    own_dispose=Mock(side_effect=AssertionError("Core disposed Global Chess"))
    monkeypatch.setattr(chess_db.engine,"dispose",own_dispose)
    await core_main.close_core_resources()
    own_dispose.assert_not_called()


@pytest.mark.parametrize("mode", ["no-core-db","distinct-engines","missing-production-url","invalid-explicit-url"])
def test_fresh_process_database_boundary(mode):
    environment=os.environ.copy()
    for key in list(environment):
        if key.startswith("GLOBAL_CHESS_") or key in {"DATABASE_URL","ENVIRONMENT"}:environment.pop(key)
    environment.update(PYTHONDONTWRITEBYTECODE="1",DATABASE_URL="sqlite://",ENVIRONMENT="test",
        GLOBAL_CHESS_ENVIRONMENT="test",GLOBAL_CHESS_DATABASE_URL="sqlite:///:memory:")
    bootstrap = """
import sys, pathlib, importlib.abc
from pydantic_settings.sources.providers.dotenv import DotEnvSettingsSource
DotEnvSettingsSource._read_env_files=lambda self:{}
sys.path.insert(0,str(pathlib.Path('backend').resolve()))
"""
    programs = {
        "no-core-db": """
class DenyCoreDatabase(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname == 'app.core.database':raise AssertionError('Core database imported')
sys.meta_path.insert(0,DenyCoreDatabase())
from app import main, models, database
assert 'app.core.database' not in sys.modules
assert len(database.Base.metadata.tables)==5
""",
        "distinct-engines": """
from app import database as chess, models
from app.core import database as core
from app.core.models import CoreBase, ApplicationDB
from sqlalchemy import inspect
assert str(chess.engine.url)!=str(core.engine.url)
chess.Base.metadata.create_all(chess.engine)
CoreBase.metadata.create_all(core.engine)
assert 'topics' not in inspect(core.engine).get_table_names()
assert 'core_sources' not in inspect(chess.engine).get_table_names()
assert chess.get_db is not core.get_db and chess.SessionLocal is not core.SessionLocal
chess.engine.dispose();core.engine.dispose()
""",
        "missing-production-url": """
from app.global_chess_config import GlobalChessDatabaseConfigurationError
try:import app.database
except GlobalChessDatabaseConfigurationError:pass
else:raise AssertionError('Production used legacy database')
""",
        "invalid-explicit-url": """
from app.global_chess_config import GlobalChessDatabaseConfigurationError
try:import app.database
except GlobalChessDatabaseConfigurationError as exc:
    assert 'private-fixture' not in str(exc)
else:raise AssertionError('Invalid explicit URL fell back')
""",
    }
    if mode=="missing-production-url":
        environment.pop("GLOBAL_CHESS_DATABASE_URL");environment["GLOBAL_CHESS_ENVIRONMENT"]="production"
    elif mode=="invalid-explicit-url":
        environment.update(GLOBAL_CHESS_ENVIRONMENT="development",ENVIRONMENT="development",
            GLOBAL_CHESS_DATABASE_URL="not-a-database-url-private-fixture")
    result=subprocess.run([sys.executable,"-B","-c",bootstrap+programs[mode]],env=environment,
        capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr


def test_no_chess_runtime_imports_core_database():
    root=Path(__file__).parents[1]/"app"
    paths=[root/"main.py",root/"database.py",root/"global_chess_config.py",
        *(root/"models").glob("*.py"),*(root/"routes").glob("*.py"),
        *(root/"agents").glob("*.py"),*(root/"clients").glob("*.py")]
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if isinstance(node,ast.ImportFrom):assert node.module!="app.core.database",str(path)
            if isinstance(node,ast.Import):assert all(item.name!="app.core.database" for item in node.names),str(path)
