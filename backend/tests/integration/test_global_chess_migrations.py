"""Reviewed Global Chess DDL against verified, empty disposable PostgreSQL."""

import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.database import Base
from app import models
from app.global_chess_readiness import MIGRATIONS, VERSION_TABLE, expected_revision, validate_global_chess_database

ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "backend/global_chess_alembic.ini"
HEAD = "e1b7c42a9d60"
TABLES = {"topics", "topic_analyses", "research_sources", "story_clusters", "story_cluster_members"}


def config(connection=None):
    result = Config(str(CONFIG))
    if connection is not None:result.attributes["connection"] = connection
    return result


def migrate(engine, revision="head"):
    with engine.begin() as connection:
        command.upgrade(config(connection), revision)


def test_independent_graph_and_static_ownership():
    scripts = ScriptDirectory.from_config(config())
    assert scripts.get_bases() == scripts.get_heads() == [HEAD]
    assert expected_revision() == HEAD
    assert [revision.revision for revision in scripts.walk_revisions()] == [HEAD]
    assert set(Base.metadata.tables) == TABLES
    assert "app.core" not in (MIGRATIONS / "env.py").read_text()
    assert "CoreBase" not in (MIGRATIONS / "env.py").read_text()
    assert "app.core" not in (MIGRATIONS / "versions/e1b7c42a9d60_initial_global_chess.py").read_text()


def test_fresh_bootstrap_exact_metadata_indexes_constraints_and_sequences(migration_engine):
    # migration_engine independently verifies a new empty PostgreSQL database and
    # forbids create_all() for both metadata owners for the whole test.
    migrate(migration_engine)
    with migration_engine.connect() as connection:
        inspector = inspect(connection)
        assert set(inspector.get_table_names(schema="public")) == TABLES | {VERSION_TABLE}
        assert connection.execute(text(f"SELECT version_num FROM public.{VERSION_TABLE}")).scalars().all() == [HEAD]
        context = MigrationContext.configure(connection, opts={
            "version_table": VERSION_TABLE, "version_table_schema": "public", "compare_type": True,
            "compare_server_default": True,
            # Alembic reflection normalizes public to schema=None. Its version
            # table is migration infrastructure, not an ORM application table.
            "include_object": lambda obj, name, type_, reflected, other: not (type_ == "table" and name == VERSION_TABLE),
        })
        assert compare_metadata(context, Base.metadata) == []
        for name, table in Base.metadata.tables.items():
            columns = inspector.get_columns(name, schema="public")
            assert [column["name"] for column in columns] == list(table.columns.keys())
            assert {column["name"]: column["nullable"] for column in columns} == {c.name: c.nullable for c in table.columns}
            assert inspector.get_pk_constraint(name, schema="public")["constrained_columns"] == ["id"]
            actual_indexes = {(i["name"], bool(i["unique"]), tuple(i["column_names"])) for i in inspector.get_indexes(name, schema="public") if not i.get("duplicates_constraint")}
            assert actual_indexes == {(i.name, bool(i.unique), tuple(c.name for c in i.columns)) for i in table.indexes}
            assert {(u["name"], tuple(u["column_names"])) for u in inspector.get_unique_constraints(name, schema="public")} == (
                {("uq_research_sources_topic_url", ("topic_id", "url"))} if name == "research_sources" else set())
            assert {(tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]), f["options"].get("ondelete"))
                    for f in inspector.get_foreign_keys(name, schema="public")} == {
                (tuple(c.name for c in f.columns), f.referred_table.name, tuple(e.column.name for e in f.elements), f.ondelete)
                for f in table.foreign_key_constraints}
            assert connection.execute(text("SELECT pg_get_serial_sequence(:table, 'id')"), {"table": "public." + name}).scalar_one() == f"public.{name}_id_seq"
            assert columns[0].get("identity") is None and "nextval(" in columns[0]["default"]
            assert all(column["default"] is None for column in columns[1:])
        command.check(config(connection))


def test_orm_defaults_generated_ids_persistence_and_database_cascades(migration_engine):
    migrate(migration_engine)
    with Session(migration_engine) as db:
        topic = models.Topic(title="Synthetic topic", url="https://example.com/topic", source="Fixture", relevance_score=10, freshness_score=20, score=13)
        cluster = models.StoryClusterRecord(title="Synthetic cluster", summary="Preserved", confidence=80)
        db.add_all([topic, cluster]);db.flush()
        analysis = models.TopicAnalysis(topic_id=topic.id, summary="Summary", relevance=50, research_priority=60, rationale="Reason", research_questions="[]", model="fixture-model")
        source = models.ResearchSource(topic_id=topic.id, title="Source", url="https://example.com/source")
        member = models.StoryClusterMember(topic_id=topic.id, cluster_id=cluster.id)
        db.add_all([analysis, source, member]);db.commit()
        assert [row.id for row in (topic, cluster, analysis, source, member)] == [1] * 5
        assert topic.status == "candidate" and topic.summary is None and source.source_type == "news"
        assert all(stamp.tzinfo is not None for stamp in (topic.fetched_at, cluster.created_at, analysis.analyzed_at, source.discovered_at))
        db.execute(text("DELETE FROM topics WHERE id=:id"), {"id": topic.id});db.commit()
        assert not db.scalars(select(models.TopicAnalysis)).all()
        assert not db.scalars(select(models.ResearchSource)).all()
        assert not db.scalars(select(models.StoryClusterMember)).all()
        assert db.scalars(select(models.StoryClusterRecord)).one().summary == "Preserved"


@pytest.mark.parametrize("first", ["core", "chess"])
def test_both_histories_and_data_are_isolated_in_either_order(migration_engine, first):
    from app.core.models import CoreBase, ApplicationDB
    from app.core.readiness import MIGRATIONS as CORE_MIGRATIONS, expected_revision as core_head, validate_database_readiness
    def core_config(connection):
        result = Config();result.set_main_option("script_location", str(CORE_MIGRATIONS));result.attributes["connection"] = connection
        return result
    def upgrade(owner):
        with migration_engine.begin() as connection:
            command.upgrade(core_config(connection) if owner == "core" else config(connection), "head")
        with Session(migration_engine) as db:
            if owner == "core":db.add(ApplicationDB(id="synthetic-core-app", name="Preserved Core", slug="preserved-core"))
            else:db.add(models.Topic(title="Preserved Chess", url="https://example.com/topic", source="Fixture", relevance_score=1, freshness_score=2, score=3, summary="Preserved"))
            db.commit()
    upgrade(first);upgrade("chess" if first == "core" else "core")
    with migration_engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) == TABLES | set(CoreBase.metadata.tables) | {VERSION_TABLE, "alembic_version"}
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == core_head()
        assert connection.execute(text(f"SELECT version_num FROM {VERSION_TABLE}")).scalar_one() == HEAD
        assert connection.execute(select(ApplicationDB.name)).scalar_one() == "Preserved Core"
        assert connection.execute(select(models.Topic.summary)).scalar_one() == "Preserved"
        command.check(config(connection));command.check(core_config(connection))
    validate_global_chess_database(migration_engine);validate_database_readiness(migration_engine)
    with migration_engine.begin() as connection:command.downgrade(config(connection), "base")
    with migration_engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) == set(CoreBase.metadata.tables) | {VERSION_TABLE, "alembic_version"}
        assert connection.execute(select(ApplicationDB.name)).scalar_one() == "Preserved Core"
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == core_head()
        assert not connection.execute(text(f"SELECT version_num FROM {VERSION_TABLE}")).all()


def test_existing_unversioned_chess_tables_are_not_adopted_or_overwritten(migration_engine):
    with migration_engine.begin() as connection:
        connection.execute(text("CREATE TABLE topics (id integer PRIMARY KEY, sentinel text)"))
        connection.execute(text("INSERT INTO topics VALUES (1, 'synthetic preserved value')"))
    with pytest.raises(RuntimeError, match="do not stamp or overwrite"):
        migrate(migration_engine)
    with migration_engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) == {"topics"}
        assert connection.execute(text("SELECT sentinel FROM topics")).scalar_one() == "synthetic preserved value"


def test_fresh_cli_environment_has_no_core_imports_or_create_all(migration_engine):
    environment = os.environ.copy()
    environment["GLOBAL_CHESS_DATABASE_URL"] = migration_engine.url.render_as_string(hide_password=False)
    environment["GLOBAL_CHESS_ALLOW_LEGACY_DATABASE_URL"] = "false"
    program = '''
import sys,importlib.abc
from pydantic_settings.sources import DotEnvSettingsSource
def forbidden_dotenv(*args,**kwargs):raise AssertionError('Migration read a dotenv file')
DotEnvSettingsSource.__call__=forbidden_dotenv
class Boundary(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname=='app.core' or fullname.startswith('app.core.') or fullname in {'app.factory','app.database','app.global_chess_config'}:raise AssertionError('Runtime/Core import during Global Chess migration')
sys.meta_path.insert(0,Boundary())
from sqlalchemy import MetaData
def forbidden(*args,**kwargs):raise AssertionError('create_all is forbidden')
MetaData.create_all=forbidden
from alembic.config import Config
from alembic import command
command.upgrade(Config('backend/global_chess_alembic.ini'),'head')
assert not any(name=='app.core' or name.startswith('app.core.') for name in sys.modules)
'''
    result = subprocess.run([sys.executable, "-B", "-c", program], cwd=ROOT, env=environment, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    validate_global_chess_database(migration_engine)


def test_missing_explicit_cli_url_cannot_use_legacy_or_dotenv(monkeypatch):
    monkeypatch.delenv("GLOBAL_CHESS_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://127.0.0.1:1/forbidden")
    with pytest.raises(RuntimeError, match="explicit GLOBAL_CHESS_DATABASE_URL"):
        command.upgrade(config(), "head")


def test_offline_sql_is_global_chess_only(monkeypatch):
    monkeypatch.setenv("GLOBAL_CHESS_DATABASE_URL", "postgresql+psycopg://127.0.0.1:1/never-connected")
    buffer = io.StringIO();cfg = config();cfg.output_buffer = buffer
    command.upgrade(cfg, "head", sql=True)
    sql = buffer.getvalue()
    assert sql.count("SERIAL NOT NULL") == 5
    assert all(f"CREATE TABLE {name} (" in sql for name in TABLES)
    assert "core_sources" not in sql and "core_applications" not in sql
    assert "public.global_chess_alembic_version" in sql
