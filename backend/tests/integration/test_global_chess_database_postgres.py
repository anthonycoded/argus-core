"""Separate engines/dependencies against two verified disposable databases only."""

import re
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app import database as chess_db
from app import models
from app.core import database as core_db
from app.core.models import ApplicationDB, CoreBase
from app.core.readiness import MIGRATIONS, validate_database_readiness


def test_database_dependencies_and_core_readiness_are_independent(migration_engine, monkeypatch):
    # migration_engine independently provisions/verifies its disposable Core DB.
    with migration_engine.begin() as connection:
        cfg=Config();cfg.set_main_option("script_location",str(MIGRATIONS));cfg.attributes["connection"]=connection
        command.upgrade(cfg,"head")
    name="argus_global_chess_test_"+uuid4().hex
    assert re.fullmatch(r"argus_global_chess_test_[0-9a-f]{32}",name)
    target=None;created=False
    try:
        with migration_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as admin:
            assert re.fullmatch(r"argus_core_migration_test_[0-9a-f]{32}",admin.execute(text("SELECT current_database()")).scalar_one())
            assert admin.engine.url.host=="127.0.0.1" and admin.engine.url.port not in {None,5432,5433}
            admin.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE template0');created=True
        target=create_engine(migration_engine.url.set(database=name),poolclass=NullPool)
        with target.connect() as connection:
            assert connection.execute(text("SELECT current_database()")).scalar_one()==name
            assert inspect(connection).get_table_names()==[]
        # Initialize solely through the application-owned migration history.
        from app.global_chess_readiness import MIGRATIONS as CHESS_MIGRATIONS, VERSION_TABLE, validate_global_chess_database
        with target.begin() as connection:
            cfg=Config();cfg.set_main_option("script_location",str(CHESS_MIGRATIONS));cfg.attributes["connection"]=connection
            command.upgrade(cfg,"head")
        validate_global_chess_database(target)
        monkeypatch.setattr(chess_db,"SessionLocal",sessionmaker(bind=target,autoflush=False,autocommit=False))
        monkeypatch.setattr(core_db,"SessionLocal",sessionmaker(bind=migration_engine,autoflush=False,autocommit=False))

        dependency=chess_db.get_db();session=next(dependency)
        try:
            assert session.bind.url.database==name
            session.add(models.Topic(id=1,title="Synthetic preserved topic",url="https://example.com/topic",
                source="Fixture",relevance_score=1,freshness_score=2,score=3,status="approved",summary="Preserved"))
            session.commit()
        finally:dependency.close()
        dependency=core_db.get_db();session=next(dependency)
        try:
            assert session.bind is migration_engine
            session.add(ApplicationDB(id="synthetic-core-app",name="Synthetic Core application",slug="synthetic-core"))
            session.commit()
        finally:dependency.close()

        with migration_engine.connect() as connection:
            assert set(inspect(connection).get_table_names())==set(CoreBase.metadata.tables)|{"alembic_version"}
            assert connection.execute(select(ApplicationDB.id)).scalar_one()=="synthetic-core-app"
        with target.connect() as connection:
            assert set(inspect(connection).get_table_names())==set(chess_db.Base.metadata.tables)|{VERSION_TABLE}
            assert connection.execute(select(models.Topic.status,models.Topic.summary)).one()==("approved","Preserved")

        statements=[]
        def record(conn,cursor,statement,parameters,context,many):statements.append(statement)
        event.listen(migration_engine,"before_cursor_execute",record)
        try:validate_database_readiness(migration_engine)
        finally:event.remove(migration_engine,"before_cursor_execute",record)
        assert statements and all(re.match(r"\s*(SELECT|SHOW)\b",sql,re.I) for sql in statements)
        with target.connect() as connection:
            assert set(inspect(connection).get_table_names())==set(chess_db.Base.metadata.tables)|{VERSION_TABLE}
    finally:
        if target is not None:target.dispose()
        if created:
            with migration_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as admin:
                assert re.fullmatch(r"argus_core_migration_test_[0-9a-f]{32}",admin.execute(text("SELECT current_database()")).scalar_one())
                admin.exec_driver_sql(f'DROP DATABASE "{name}"')  # No FORCE: leaked connections fail visibly.
                assert not admin.execute(text("SELECT 1 FROM pg_database WHERE datname=:name"),{"name":name}).scalar()
