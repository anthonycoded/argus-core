"""Real Alembic execution in newly created, uniquely named disposable databases.

Never uses postgres_test_engine: that fixture initializes tables with create_all.
"""

import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError

from app import models as chess_models
from app.database import Base
from app.core.models import CoreBase

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "backend/migrations"
BASELINE = "3f1a9c2d7e60"
OLD_ROOT = "8d3f39d3a946"
PRE_ADMISSION_HEAD = "dea3e0498bf6"
HEAD = "61c2a9e47b30"
OLD_REVISIONS = (OLD_ROOT, "8bd61bcfd002", PRE_ADMISSION_HEAD)


def configuration(connection=None, location=MIGRATIONS):
    cfg = Config()
    cfg.set_main_option("script_location", str(location))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def assert_core_matches(connection):
    def owned(obj, name, kind, reflected, compare_to):
        return kind != "table" or name in CoreBase.metadata.tables
    context = MigrationContext.configure(connection, opts={"compare_type": True, "include_object": owned})
    assert compare_metadata(context, CoreBase.metadata) == []
    assert set(inspect(connection).get_table_names()) >= set(CoreBase.metadata.tables)
    assert connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == [HEAD]


def create_chess_data(connection):
    # Application schema is a pre-existing fixture, not part of Core migrations.
    for table in Base.metadata.sorted_tables:
        table.create(connection)
    connection.execute(chess_models.Topic.__table__.insert().values(
        id=1, title="Preserve editorial topic", url="https://example.test/topic",
        source="fixture", relevance_score=1, freshness_score=2, score=3, status="approved"))
    connection.execute(chess_models.TopicAnalysis.__table__.insert().values(
        id=1, topic_id=1, summary="Preserve analysis", relevance=1, research_priority=2,
        rationale="fixture", research_questions="[]", model="fixture"))
    connection.execute(chess_models.ResearchSource.__table__.insert().values(
        id=1, topic_id=1, title="Preserve source", url="https://example.test/research", snippet="fixture"))
    connection.execute(chess_models.StoryClusterRecord.__table__.insert().values(
        id=1, title="Preserve cluster", summary="fixture", confidence=1))
    connection.execute(chess_models.StoryClusterMember.__table__.insert().values(id=1, cluster_id=1, topic_id=1))


def snapshot(connection, table_names):
    inspector = inspect(connection)
    result = {}
    for name in sorted(table_names):
        table = CoreBase.metadata.tables.get(name)
        if table is None:
            table = Base.metadata.tables[name]
        result[name] = {
            "rows": [tuple(row) for row in connection.execute(select(table).order_by(*table.primary_key.columns))],
            "oid": connection.execute(text("SELECT to_regclass(:name)::oid"), {"name": name}).scalar(),
            "columns": [(c["name"], str(c["type"]), c["nullable"], c["default"]) for c in inspector.get_columns(name)],
            "pk": inspector.get_pk_constraint(name),
            "foreign_keys": inspector.get_foreign_keys(name),
            "unique_constraints": inspector.get_unique_constraints(name),
            "indexes": inspector.get_indexes(name),
        }
    return result


def legacy_source_schema(connection):
    # Frozen historical schema, independent of current ORM or new migration.
    connection.exec_driver_sql("""CREATE TABLE core_sources (
        id varchar(36) PRIMARY KEY, title varchar(1000) NOT NULL,
        url text NOT NULL, canonical_url text NOT NULL, domain varchar(255) NOT NULL,
        content_hash varchar(64), publisher varchar(500), published_at timestamptz,
        source_type varchar(100) NOT NULL, metadata json NOT NULL, created_at timestamptz NOT NULL,
        CONSTRAINT uq_core_sources_canonical_url UNIQUE(canonical_url))""")
    connection.exec_driver_sql("CREATE INDEX ix_core_sources_content_hash ON core_sources(content_hash)")
    connection.exec_driver_sql("CREATE INDEX ix_core_sources_domain ON core_sources(domain)")


def seed_core(connection):
    existing = set(inspect(connection).get_table_names())
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    connection.execute(CoreBase.metadata.tables["core_sources"].insert().values(
        id="source-fixture", title="Preserve source", url="https://example.test/article",
        canonical_url="https://example.test/article", domain="example.test", content_hash="a" * 64,
        source_type="fixture", metadata={"preserve": True}, created_at=now))
    if "core_applications" in existing:
        connection.execute(CoreBase.metadata.tables["core_applications"].insert().values(
            id="application-fixture", name="Preserve application", slug="fixture", is_active=True, created_at=now))
        connection.execute(CoreBase.metadata.tables["core_api_keys"].insert().values(
            id="key-fixture", application_id="application-fixture", name="Preserve key",
            key_prefix="fixture-prefix", key_hash="b" * 64, scopes=["sources:read"], is_active=True, created_at=now))
    if "core_audit_logs" in existing:
        connection.execute(CoreBase.metadata.tables["core_audit_logs"].insert().values(
            id="audit-fixture", application_id="application-fixture", api_key_id="key-fixture",
            method="GET", path="/api/v1/sources", status_code=200, duration_ms=12, created_at=now))


def test_migration_graph_preserves_existing_revision_ids():
    scripts = ScriptDirectory.from_config(configuration())
    assert scripts.get_bases() == [BASELINE]
    assert scripts.get_heads() == [HEAD]
    assert [revision.revision for revision in scripts.walk_revisions()] == [HEAD, PRE_ADMISSION_HEAD, OLD_REVISIONS[1], OLD_ROOT, BASELINE]


def test_empty_postgres_initializes_entirely_through_alembic(migration_engine):
    with migration_engine.begin() as connection:
        cfg = configuration(connection)
        command.upgrade(cfg, "head")
        assert set(inspect(connection).get_table_names()) == {*CoreBase.metadata.tables, "alembic_version"}
        assert_core_matches(connection)
        command.check(cfg)


def test_historical_baseline_has_expected_nonunique_index(migration_engine):
    with migration_engine.begin() as connection:
        cfg = configuration(connection)
        command.upgrade(cfg, BASELINE)
        assert set(inspect(connection).get_table_names()) == {"core_sources", "alembic_version"}
        indexes = {i["name"]: i for i in inspect(connection).get_indexes("core_sources")}
        assert indexes["ix_core_sources_content_hash"]["unique"] is False
        assert "uq_core_sources_content_hash" not in indexes
        command.upgrade(cfg, "head")
        assert_core_matches(connection)


@pytest.mark.parametrize("old_revision", OLD_REVISIONS)
def test_existing_revision_preserves_core_and_chess_data(migration_engine, tmp_path, old_revision):
    old_scripts = tmp_path / "old_migrations"
    shutil.copytree(MIGRATIONS, old_scripts, ignore=shutil.ignore_patterns("__pycache__"))
    (old_scripts / f"versions/{BASELINE}_create_historical_core_sources.py").unlink()
    original_root = old_scripts / f"versions/{OLD_ROOT}_add_unique_content_hash_index.py"
    content = original_root.read_text()
    assert f'down_revision = "{BASELINE}"' in content
    original_root.write_text(content.replace(f'down_revision = "{BASELINE}"', "down_revision = None"))
    old_cfg = configuration(location=old_scripts)
    assert ScriptDirectory.from_config(old_cfg).get_bases() == [OLD_ROOT]
    with migration_engine.begin() as connection:
        legacy_source_schema(connection)
        create_chess_data(connection)
        command.upgrade(configuration(connection, old_scripts), old_revision)
        seed_core(connection)
        existing = (set(CoreBase.metadata.tables) | set(Base.metadata.tables)) & set(inspect(connection).get_table_names())
        before = snapshot(connection, existing)
    with migration_engine.begin() as connection:
        command.upgrade(configuration(connection), "head")
    with migration_engine.begin() as connection:
        assert snapshot(connection, existing) == before
        assert_core_matches(connection)
        assert connection.execute(text("SELECT count(*) FROM core_source_catalogs")).scalar_one() == 0
        assert connection.execute(text("SELECT count(*) FROM core_source_admission_events")).scalar_one() == 0
        # Exercise env.py's actual ownership filter with populated Chess tables.
        command.check(configuration(connection))


def test_core_upgrade_and_disposable_downgrade_leave_chess_untouched(migration_engine):
    with migration_engine.begin() as connection:
        create_chess_data(connection)
        before = snapshot(connection, Base.metadata.tables)
        cfg = configuration(connection)
        command.upgrade(cfg, "head")
        assert_core_matches(connection)
        command.check(cfg)
        assert snapshot(connection, Base.metadata.tables) == before
        command.downgrade(cfg, "base")
        assert snapshot(connection, Base.metadata.tables) == before
        assert not (set(CoreBase.metadata.tables) & set(inspect(connection).get_table_names()))


def test_duplicates_fail_without_changing_or_deleting_data(migration_engine):
    with migration_engine.begin() as connection:
        command.upgrade(configuration(connection), BASELINE)
        seed_core(connection)
        connection.execute(CoreBase.metadata.tables["core_sources"].insert().values(
            id="duplicate-fixture", title="Preserve duplicate", url="https://example.test/duplicate",
            canonical_url="https://example.test/duplicate", domain="example.test", content_hash="a" * 64,
            source_type="fixture", metadata={}))
        before = snapshot(connection, ["core_sources"])
    with pytest.raises(IntegrityError):
        with migration_engine.begin() as connection:
            command.upgrade(configuration(connection), "head")
    with migration_engine.connect() as connection:
        assert snapshot(connection, ["core_sources"]) == before
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == BASELINE


def test_explicit_config_url_overrides_application_settings(migration_engine):
    cfg = configuration()
    cfg.set_main_option("sqlalchemy.url", migration_engine.url.render_as_string(hide_password=False).replace("%", "%%"))
    command.upgrade(cfg, "head")
    with migration_engine.connect() as connection:
        assert_core_matches(connection)


def test_alembic_cli_works_from_repository_root(migration_engine):
    environment = os.environ.copy()
    environment["DATABASE_URL"] = migration_engine.url.render_as_string(hide_password=False)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-B", "-m", "alembic", "-c", str(ROOT / "alembic.ini"), "upgrade", "head"],
        cwd=ROOT, env=environment, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    with migration_engine.connect() as connection:
        assert_core_matches(connection)
