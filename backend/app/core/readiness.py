"""Read-only startup validation. Schema changes belong to deployment migrations."""

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.core.models import CoreBase

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
MIGRATION_HINT = (
    "Provision the intended database, then run 'python -m alembic -c alembic.ini upgrade head' "
    "as a separate deployment step before starting Argus-Core. "
    "Populated unversioned or inconsistent databases require reviewed repair; do not blindly stamp them."
)


class DatabaseReadinessError(RuntimeError):
    pass


def expected_revision() -> str:
    # ScriptDirectory reads revision declarations, not env.py or migration bodies.
    # Never invoke Alembic's upgrade/stamp/autogeneration APIs from startup.
    try:
        config = Config()
        config.set_main_option("script_location", str(MIGRATIONS))
        heads = ScriptDirectory.from_config(config).get_heads()
        if len(heads) != 1:
            raise ValueError("Expected one migration head")
        return heads[0]
    except Exception:
        raise DatabaseReadinessError(
            "Cannot determine the Argus-Core schema revision. Deploy the migration files with exactly one head."
        ) from None


def validate_database_readiness(engine: Engine) -> None:
    expected = expected_revision()
    if engine.dialect.name != "postgresql":
        raise DatabaseReadinessError("Argus-Core requires a PostgreSQL database. Check DATABASE_URL.")
    try:
        with engine.connect() as connection:
            # Characteristics are scoped to this checkout and reset by SQLAlchemy
            # on return to the pool. Closing the connection rolls back this check.
            connection = connection.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True,
            )
            if connection.execute(text("SELECT current_schema()")).scalar() != "public":
                raise DatabaseReadinessError(
                    "Argus-Core expects public as the current database schema. "
                    "Align the migration and application roles' search_path."
                )
            tables = set(inspect(connection).get_table_names(schema="public"))
            if "alembic_version" not in tables:
                raise DatabaseReadinessError("Database migration history is missing. " + MIGRATION_HINT)
            revisions = connection.execute(text("SELECT version_num FROM public.alembic_version")).scalars().all()
            if not revisions:
                raise DatabaseReadinessError("Database has no migration revision record. " + MIGRATION_HINT)
            if revisions != [expected]:
                # Do not echo arbitrary database values or connection details.
                raise DatabaseReadinessError(
                    f"Database is not at the expected Argus-Core Alembic revision {expected}. "
                    "If this database belongs to a newer release, deploy matching application code. " + MIGRATION_HINT
                )
            missing = sorted(set(CoreBase.metadata.tables) - tables)
            if missing:
                raise DatabaseReadinessError(
                    "Database is missing required Core tables: " + ", ".join(missing) + ". "
                    "Check the target database and investigate schema drift; an already-head database "
                    "needs a reviewed repair rather than another upgrade-to-head."
                )
    except (SQLAlchemyError, OSError, TimeoutError):
        raise DatabaseReadinessError(
            "Argus-Core database is unavailable or cannot be inspected. "
            "Check DATABASE_URL, PostgreSQL availability, and the runtime role's read permissions."
        ) from None
