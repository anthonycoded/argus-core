# Global Chess database ownership — Phase 6B

Global Chess owns `app.database.Base`, `engine`, `SessionLocal`, and `get_db`.
Models keep their existing imports and exact schema: topics, topic_analyses,
research_sources, story_clusters and story_cluster_members. No Core ORM relationship,
table, index, constraint, row, migration or normalization behavior is changed.
Core continues using `app.core.database` and its separate six-table CoreBase.
The old `app.core.database.Base` remains as an empty compatibility class; Global
Chess models do not register there. No Global Chess runtime code imports Core's
database module.

## Configuration and intermediate compatibility

`GLOBAL_CHESS_DATABASE_URL` is the explicit application-owned connection setting.
It takes precedence over generic `DATABASE_URL` and is secret-masked in settings.
The database module imports only Global Chess configuration, not Core settings.
URLs are never logged and configuration errors do not echo connection strings.
An explicit blank or malformed URL fails rather than falling back.

To preserve the existing development target during this intermediate phase,
`GLOBAL_CHESS_ALLOW_LEGACY_DATABASE_URL` defaults to true. Fallback is permitted
**only** when `GLOBAL_CHESS_ENVIRONMENT` is exactly development (the default) and
generic `ENVIRONMENT` is absent or development. Values are case/whitespace
normalized. Test, staging, production, prod and unknown environments cannot fall
back; either environment indicator can prohibit it. Even an enabled compatibility
flag cannot override that restriction. Production deployments must explicitly
identify their environment; an omitted environment still means development.

Development fallback reads the existing `DATABASE_URL` value independently through
Global Chess's existing settings source. It uses the exact value without choosing
a new host/name or modifying it. A sanitized warning announces compatibility use
and the explicit setting/disable flag. There is no automatic database cutover.
Set `GLOBAL_CHESS_ALLOW_LEGACY_DATABASE_URL=false` to require an explicit URL locally
too. This fallback is transitional through Phase 6E, not a permanent production path.

The existing settings dotenv source path is unchanged to preserve local operation;
this task does not inspect, edit or create environment files. Validation disables
dotenv reads and supplies isolated process settings. An operator can later set the
explicit Global Chess URL to the **same independently verified current target** to
retire fallback without moving data. Selecting a new database is a separate approved
cutover requiring backups, restore rehearsal and integrity verification.

`GlobalChessSettings` can be used by the HTTP client without initializing a
database. Importing the database/application requires a resolvable URL and fails
clearly if missing outside development compatibility. No database is contacted
merely to parse settings or create the lazy SQLAlchemy engine. Distinct engines
do not by themselves mean distinct storage: development fallback may intentionally
point both applications at the existing shared database until an approved cutover.

## Session and application lifecycle

Global Chess's session factory retains `autoflush=False` and `autocommit=False`.
`get_db` creates/closes only an application-owned session; routes retain their
existing commits, rollbacks, status/summary preservation and deduplication. Closing
an uncommitted session rolls back its pending transaction. There is no global
close-all operation that could close Core sessions.

`app.main` uses the Global Chess Base/engine, and its shutdown callback disposes
only that engine through the existing FastAPI lifespan/finally behavior. Startup
failure still runs cleanup. Core cannot dispose this engine through its shutdown
callback. Request sessions must finish during normal server request draining;
engine disposal is not a mechanism for interrupting active transactions.

**Startup `create_all()` remains temporary until Phase 6E.** This phase preserves
that existing behavior for the five Global Chess tables and adds no migration
system or automatic migrations. It does not authorize starting the application
against an unverified target. Core startup remains read-only and keeps the exact
Alembic-head/table readiness checks. All Core migrations and CoreBase ownership
are unchanged.

## Validation and remaining extraction work

The frozen pre-refactor PostgreSQL DDL fixture verifies all five table/index
definitions remain identical. Tests verify separate metadata, engines, session
dependencies, session rollback/close, production failure, warning sanitization,
fresh-process imports with Core database access denied, and owned shutdown.
PostgreSQL tests use separate uniquely named disposable databases and demonstrate
that Core readiness creates no Global Chess tables. Existing API/persistence,
ingestion, analysis, research, Core migration and readiness tests remain regressions.

Phase 6C removes the shared factory/settings dependency and supplies an application-
owned temporary direct classifier client; see
[GLOBAL_CHESS_STARTUP.md](GLOBAL_CHESS_STARTUP.md). Relevance classification still
uses direct Ollama inference and normalization remains shared.
Independent migrations/readiness, packaging, access protection and the
database data cutover require later milestones. Source registration remains unused
by research/ingestion and GDELT remains disabled. No credentials, environment files,
production/development databases or existing historical records are changed here.
