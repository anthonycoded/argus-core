# Argus-Core database startup

Deploy in this order:

1. Provision the intended PostgreSQL database and roles. Verify its host, port and
   name. Back up existing data and review any incompatible/unversioned schema.
2. Select that target explicitly in the deployment environment and run
   `python -m alembic -c alembic.ini upgrade head` from the repository root with
   migration-role credentials. This is a separate deployment job; it must finish
   before starting application workers. Never use a development target for tests.
3. Start `uvicorn app.core_main:app --app-dir backend` with the runtime role and
   the same intended database. Deploy the migration files alongside the app.

Core startup no longer calls `CoreBase.metadata.create_all()`, upgrades migrations,
stamps a revision, or repairs schemas. The asynchronous lifespan readiness hook
runs blocking checks in a worker thread and requires:

- PostgreSQL connectivity and sufficient catalog/version-table read access.
- `public` as the current schema, consistent with the existing migration layout.
- Exactly one deployed Alembic head and exactly that revision in
  `public.alembic_version`.
- All tables registered in CoreBase, currently core_sources, core_applications,
  core_api_keys and core_audit_logs, in public.

Database inspection uses a REPEATABLE READ, READ ONLY PostgreSQL transaction and
rolls back on connection close. SQLAlchemy resets checkout characteristics before
returning the connection to its pool. Only SELECT/catalog inspection is performed.
Missing history, missing revision records, older/unknown/multiple revisions,
missing tables, and connection/permission failures prevent request serving with a
sanitized actionable error. No database URL, credentials or arbitrary stored
revision values are included in these errors.

An upgrade-to-head does not repair a missing table in a database already recorded
at head. Investigate drift and use a reviewed migration/restore procedure. Do not
blindly stamp unversioned populated databases or recreate populated tables.

Shutdown callbacks still run if startup validation fails: Redis and the audit
pool are closed as before. Global Chess (`app.main`) retains its separate,
existing Base.metadata.create_all() initialization behavior. Other factory users
may continue to use the existing synchronous initialization callback; the new
asynchronous check hook is optional.

## Limits and operational considerations

This is a startup gate, not a continuous liveness probe or a full schema diff.
It checks migration identity and table presence, not every column, constraint,
index, or runtime write permission. Migration integration tests verify metadata
equivalence. Runtime-role privileges should separately allow the required API and
audit operations while withholding DDL permissions. Database loss or schema drift
after startup remains a runtime concern. Connection/pool/network timeouts depend
on the configured application engine and PostgreSQL driver.

Readiness tests use unique disposable PostgreSQL databases built through Alembic,
with create_all explicitly forbidden. They exercise the actual Core lifespan,
read-only behavior, revision/table failures, errors without leaked secrets, and
shutdown cleanup. The shared migration fixture is also used by bootstrap tests.
