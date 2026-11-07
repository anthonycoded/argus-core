# Global Chess database ownership and deployment ? Phase 6E

Global Chess owns five application tables: `topics`, `topic_analyses`,
`research_sources`, `story_clusters` and `story_cluster_members`. Core owns six
other tables and retains its existing migration directory, revisions and startup
behavior. There are no cross-application foreign keys.

## Metadata and application resources

`app.global_chess_base.Base` is metadata-only and imports no settings, engines or
Core modules. Models use that Base. `app.database.Base` remains a compatibility
export of the same class; existing consumers retain the same metadata object.
`app.database` owns the Global Chess engine, SessionLocal and get_db dependency.
Its autoflush=False/autocommit=False behavior, route commits, rollback and session
close semantics are unchanged. No model column, index, constraint or default was
changed. The pre-existing frozen PostgreSQL DDL fixture remains authoritative.

The baseline uses PostgreSQL SERIAL primary keys and their owned sequences.
Python-side defaults (candidate status, news source type and UTC timestamps) stay
in the ORM. They were not server defaults in the existing schema and are not
silently introduced as server defaults by migrations.

## Independent migration history

- Config: `backend/global_chess_alembic.ini`.
- Scripts: `backend/global_chess_migrations/`.
- Initial root/head: `e1b7c42a9d60`.
- Version table: `public.global_chess_alembic_version`.
- Target metadata: the five Global Chess tables only.

Core continues using root `alembic.ini`, `backend/migrations/`, and its existing
`public.alembic_version` table. Neither history imports or copies the other.
Global Chess autogeneration/check filters exclude Core, unrelated tables and
non-public schemas. Explicit reviewed revisions remain necessary for changes.
No autogeneration, upgrade, downgrade or stamp runs during application startup.

The Global Chess migration environment loads only metadata/model modules. It does
not initialize application engines or import runtime settings, CoreBase or Core
configuration. Its CLI accepts **GLOBAL_CHESS_DATABASE_URL from the process
environment only**, or a trusted programmatically injected SQLAlchemy connection.
It does not select DATABASE_URL, dotenv values or an INI connection string.
Injected connections use caller-owned transactions. CLI migrations use one
transaction and dispose their own temporary engine. Online migration roles must
have public as their current schema.

## Fresh V1 provisioning and explicit configuration

Existing Global Chess historical data does not need transferring for V1. Provision
an empty, independently owned database, such as `global_chess_v1`, outside this
repository task. Leave the existing studio-ai database and all its data intact.
Do not point the baseline at historical application tables or stamp them into the
new history. Existing tables cause the transactional baseline to fail rather than
being adopted or overwritten.

Use a dedicated migration role that owns the new database/schema and a separate
runtime role. Set authentication through your normal secret-management process;
no real credentials belong in this document, INI, source control or logs. Grant
the runtime role CONNECT, USAGE on public, required table SELECT/INSERT/UPDATE/DELETE,
and sequence USAGE for generated IDs. Readiness also needs SELECT on the Global
Chess version table. Avoid granting runtime DDL privileges. Arrange equivalent
default privileges for future migrations, and explicitly align both roles'
search_path with public. Provisioning/grants are operator work, not application
startup actions; this task creates no permanent database or roles.

`GLOBAL_CHESS_DATABASE_URL` is authoritative and secret-masked. Set the deployment
environment explicitly, including in production. Prefer PostgreSQL's psycopg
SQLAlchemy URL driver. The existing development-only DATABASE_URL parser remains
for temporary configuration/script compatibility and warns when used; production
still rejects fallback. **Serving Global Chess now requires an explicit Global
Chess database setting before readiness can connect**, even in development.
Set GLOBAL_CHESS_ALLOW_LEGACY_DATABASE_URL=false to retire fallback parsing too.
No new destination is selected automatically, and no environment files are edited.
Validation disables dotenv and supplies verified disposable process settings.

## Deployment order: provision, migrate, start

From the repository root, after supplying the explicit URL securely:

```text
python -m alembic -c backend/global_chess_alembic.ini heads
python -m alembic -c backend/global_chess_alembic.ini upgrade head
python -m alembic -c backend/global_chess_alembic.ini current
uvicorn --app-dir backend app.main:app
```

From backend/, use `-c global_chess_alembic.ini` instead. The config resolves its
paths relative to itself, not the shell's current directory. Core deployment uses
its existing separate configuration and database. These commands are deployment
instructions, not authorization to run against an unverified database.

## Read-only startup and shutdown

Global Chess lifespan awaits an application-owned readiness callback in the
threadpool. It verifies PostgreSQL connectivity, current schema public, exactly
the deployed Global Chess head in its own version table, and all required tables.
Queries run in a read-only repeatable-read transaction and are rolled back/reset
when the connection returns to its pool. The check never creates tables, writes
revision records or executes Alembic mutations. Its ScriptDirectory inspection
reads revision declarations only.

Missing history/records/tables, unknown/multiple/outdated revisions, wrong schema
or unavailable PostgreSQL stop startup with sanitized, actionable errors. Core's
revision alone cannot satisfy this gate. Shutdown still disposes only the Global
Chess engine, including when startup fails. Core readiness/shutdown is unchanged.

This gate is not continuous monitoring or a full column/index drift audit. Tests
prove full fresh-schema equivalence; operators must investigate later drift.
Missing tables in an already-head database require reviewed repair, not repeated
upgrade-to-head. Unknown/ahead revisions require matching deployment code, never
blind stamping. Migrations must be deployed with exactly one root and one head.

## Recovery and rollback

Use backups/restore rehearsal before future migrations of populated databases.
Prefer a compatible application rollback over destructive schema downgrade. The
initial revision's downgrade drops the five application tables and their data,
child-first, without CASCADE; it is not a data-preserving rollback mechanism.
The separate Global Chess version table remains empty after downgrade to base.
Core tables, data and version records are untouched. Any historical-data adoption
would be a separately reviewed project; it is neither required nor performed here.

## Isolation tests and repository extraction

Integration tests use newly created, identity-verified disposable PostgreSQL
children of the dedicated argus_core_test control database. They forbid create_all,
run the actual Alembic baseline, compare all five tables against metadata, inspect
FKs/indexes/constraints/SERIAL sequences, exercise defaults/cascades/persistence,
and verify both migration orders and independent histories without touching Core
data. Readiness tests capture SELECT/SHOW-only SQL, verify READ ONLY and pool reset,
and exercise healthy/failing lifespan cleanup. CLI tests forbid Core/runtime-settings
imports and dotenv reads. Existing API/OpenAPI, research, ingestion and analysis
contracts remain regressions.

For extraction, move the application-owned migration directory/config, metadata,
models, readiness and factory together and preserve the revision ID/version-table
name. Core remains an authenticated HTTP dependency. Independent packaging,
deployment/secrets, public-access protection and final shared-utility ownership
still need review. Relevance uses Core HTTP; automatic source registration and
research GDELT remain disabled. No Core history, historical records or permanent
database was changed during this phase.
