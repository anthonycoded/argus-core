"""Global Chess-owned, read-only PostgreSQL startup gate."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from app.global_chess_base import Base
from app import models  # noqa: F401

MIGRATIONS = Path(__file__).resolve().parents[1] / "global_chess_migrations"
VERSION_TABLE = "global_chess_alembic_version"
MIGRATION_HINT = (
    "Provision a fresh Global Chess PostgreSQL database, set GLOBAL_CHESS_DATABASE_URL explicitly, "
    "then run 'python -m alembic -c backend/global_chess_alembic.ini upgrade head' before starting Global Chess. "
    "Do not stamp, recreate, or import historical application tables automatically."
)


class GlobalChessReadinessError(RuntimeError):
    pass


def expected_revision():
    try:
        config = Config()
        config.set_main_option("script_location", str(MIGRATIONS))
        scripts = ScriptDirectory.from_config(config)
        heads = scripts.get_heads()
        if len(heads) != 1 or len(scripts.get_bases()) != 1:
            raise ValueError
        return heads[0]
    except Exception:
        raise GlobalChessReadinessError(
            "Cannot determine the Global Chess schema revision. Deploy its migration files with exactly one root and one head."
        ) from None


def validate_global_chess_database(engine):
    expected = expected_revision()
    if engine.dialect.name != "postgresql":
        raise GlobalChessReadinessError("Global Chess requires PostgreSQL. Check GLOBAL_CHESS_DATABASE_URL.")
    try:
        with engine.connect() as connection:
            connection = connection.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
            if connection.execute(text("SELECT current_schema()")).scalar() != "public":
                raise GlobalChessReadinessError("Global Chess expects public as the current schema. Check role search_path.")
            tables = set(inspect(connection).get_table_names(schema="public"))
            if VERSION_TABLE not in tables:
                raise GlobalChessReadinessError("Global Chess migration history is missing. " + MIGRATION_HINT)
            revisions = connection.execute(text("SELECT version_num FROM public.global_chess_alembic_version")).scalars().all()
            if not revisions:
                raise GlobalChessReadinessError("Global Chess has no migration revision record. " + MIGRATION_HINT)
            if revisions != [expected]:
                raise GlobalChessReadinessError(
                    f"Database is not at the expected Global Chess Alembic revision {expected}. "
                    "Deploy matching code if the database belongs to a newer release. " + MIGRATION_HINT
                )
            missing = sorted(set(Base.metadata.tables) - tables)
            if missing:
                raise GlobalChessReadinessError(
                    "Database is missing required Global Chess tables: " + ", ".join(missing) + ". "
                    "Check the explicit target and investigate schema drift; an already-head database requires reviewed repair."
                )
    except (SQLAlchemyError, OSError, TimeoutError):
        raise GlobalChessReadinessError(
            "Global Chess database is unavailable or cannot be inspected. Check GLOBAL_CHESS_DATABASE_URL, "
            "PostgreSQL availability, and the runtime role's read permissions."
        ) from None
