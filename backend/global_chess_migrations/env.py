"""Global Chess-only migrations. Core's environment/history is independent."""

import os

from alembic import context
from sqlalchemy import create_engine, pool, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

config = context.config
provided_connection = config.attributes.get("connection")
database_url = os.environ.get("GLOBAL_CHESS_DATABASE_URL")

if provided_connection is None:
    if not database_url or not database_url.strip():
        raise RuntimeError("Global Chess migrations require explicit GLOBAL_CHESS_DATABASE_URL; no legacy URL fallback is permitted.")
    try:
        parsed = make_url(database_url)
        if parsed.get_backend_name() != "postgresql" or not parsed.database:
            raise ValueError
    except (ValueError, SQLAlchemyError):
        raise RuntimeError("Global Chess migrations require an explicit, valid PostgreSQL GLOBAL_CHESS_DATABASE_URL.") from None

# Metadata imports do not load application settings, dotenv, engines or Core.
from app.global_chess_base import Base  # noqa: E402
from app import models  # noqa: E402,F401

VERSION_TABLE = "global_chess_alembic_version"
target_metadata = Base.metadata


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name in {None, "public"}
    if type_ == "table":
        return name in target_metadata.tables
    return True


def include_object(object, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in target_metadata.tables
    table = getattr(object, "table", None)
    return table is None or table.name in target_metadata.tables


def options():
    return dict(target_metadata=target_metadata, version_table=VERSION_TABLE,
                version_table_schema="public", include_name=include_name,
                include_object=include_object, include_schemas=True,
                compare_type=True, compare_server_default=True)


def migrate_connection(connection):
    if connection.dialect.name != "postgresql":
        raise RuntimeError("Global Chess migrations require PostgreSQL.")
    if connection.execute(text("SELECT current_schema()")).scalar() != "public":
        raise RuntimeError("Global Chess migrations expect public as the current schema; check the migration role's search_path.")
    context.configure(connection=connection, **options())
    with context.begin_transaction():
        context.run_migrations()


def run_online():
    if provided_connection is not None:
        migrate_connection(provided_connection)
        return
    engine = create_engine(database_url, poolclass=pool.NullPool, connect_args={"connect_timeout": 3})
    try:
        with engine.begin() as connection:
            migrate_connection(connection)
    finally:
        engine.dispose()


try:
    if context.is_offline_mode():
        context.configure(url=database_url, literal_binds=True,
                          dialect_opts={"paramstyle": "named"}, **options())
        with context.begin_transaction():
            context.run_migrations()
    else:
        run_online()
except SQLAlchemyError:
    raise RuntimeError(
        "Global Chess migration failed. Verify the explicit target, role permissions and schema. "
        "The initial revision requires empty application tables; do not stamp or overwrite a populated database."
    ) from None
