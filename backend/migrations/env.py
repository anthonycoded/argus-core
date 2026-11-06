
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings
from app.core.models.base import CoreBase
from app.core.models import (
    APIKeyDB,
    ApplicationDB,
    SourceRecordDB,
    AuditLogDB,
)  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Argus-Core owns only the tables registered in CoreBase.
target_metadata = CoreBase.metadata


def include_object(
    object,
    name,
    type_,
    reflected,
    compare_to,
):
    """Restrict Alembic migrations to Argus-Core-owned tables."""

    if type_ == "table":
        return name in target_metadata.tables

    if type_ == "column":
        table = object.table

        if table.name not in target_metadata.tables:
            return False

    return True


provided_connection = config.attributes.get("connection")
database_url = config.get_main_option("sqlalchemy.url")
if not database_url:
    database_url = (
        provided_connection.engine.url.render_as_string(hide_password=False)
        if provided_connection is not None
        else settings.database_url
    )

if not database_url:
    raise RuntimeError("DATABASE_URL is missing from the Argus-Core environment.")

config.set_main_option(
    "sqlalchemy.url",
    database_url.replace("%", "%%"),
)


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def migrate_connection(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    if provided_connection is not None:
        migrate_connection(provided_connection)
        return
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        with connectable.connect() as connection:
            migrate_connection(connection)
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
