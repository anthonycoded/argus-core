# Core tests before legacy application removal

Phase 6I.1 retains all legacy application files but removes Core test imports of
their implementation. No runtime modules, API contracts or migration files change.

Run the Core-owned selection with `python -m pytest -c pytest-core.ini`.
The selection includes all 36 existing Core test modules plus a standalone
composition regression. Its conftest import guard rejects legacy application
models, databases, settings, routes, agents, clients and editorial services during
collection and execution. This is a separate selection; the original default
suite and legacy application tests remain for the later removal milestone.

## Preserved boundaries

- Migration bootstrap tests use the frozen test-owned
  `tests/fixtures/unrelated_application_schema.json`, not application ORM imports.
  It preserves the five historical table shapes, FKs, unique constraints, indexes
  and PostgreSQL SERIAL sequences. It is deliberately independent of future
  consumer model changes. SQLite no-persistence tests use INTEGER for SERIAL.
- Populated unrelated tables retain the existing before/after snapshot checks:
  rows, OIDs, column types/nullability/defaults, PKs, FKs, unique constraints and
  indexes. Core upgrades, downgrade, historical-head adoption and autogenerate
  ownership checks still execute real Alembic in disposable databases. Empty
  installs must contain exactly Core tables and its version table.
- Migration fixtures now forbid MetaData.create_all globally, without importing
  a second application's Base. Explicit sentinel DDL is test setup, never runtime
  initialization. Core metadata remains the six Core-owned tables.
- Google News/GDELT API tests retain five unrelated tables and assert no source or
  unrelated-table persistence. Existing SQL write guards remain in place.
- RSS quota tests issue authenticated Core HTTP requests directly, collecting both
  seven-page snapshots in order. They still assert 15 general, 3 discovery and 12
  continuation charges per application/key, two provider fetches, and 15 audit rows.
- Readiness and audit tests use Core-owned factory/composition assertions. They no
  longer import the legacy application merely to assert absence of schema writes
  or audit middleware. Healthy/failing startup, read-only SQL, pool reset, revision
  checks, opt-in auditing and cleanup assertions remain.

Producer/client integration tests that themselves exercise legacy Global Chess
belong to the later deletion/reclassification milestone. Those files have not been
changed here. Negative import guards may name legacy modules; frozen sentinel
table names are historical fixtures, not application dependencies.

## Dependencies and safe validation

`redis==8.1.0` matches the already-installed and previously exercised Python
client. Core uses redis.asyncio and redis.exceptions in its connection, rate-limit,
AI admission, GDELT admission and RSS snapshot implementations. No other declared
dependency is upgraded or pruned in this milestone.

Validate dependency declarations in a fresh temporary virtual environment with
the source-tested versions constrained, then run pip check. Redis server image
versions are separate from this Python-client pin.

Never supply development/production service URLs to tests. PostgreSQL integration
fixtures expect the disposable argus_core_test control database and create/drop
unique children. RSS/AI/GDELT/discovery Redis fixtures require disposable loopback
instances and, where supported, independently verified run IDs, disabled RDB/AOF
persistence, and empty test namespaces. These existing fixtures are not a blanket
guarantee that an arbitrary configured host is disposable: independently verify
container ownership/identity before running. Quota tests also use the configured
Core Redis URL; set it explicitly to the same verified test instance.

Validation must disable dotenv, reject unexpected network/DNS/database targets,
mock providers, verify cleanup, and remove task-owned disposable resources. No
independent Global Chess repository or persistent database is needed or modified.

## Phase 6I.1 validation

Fresh Python 3.12 environment: declared requirements installed with source-tested
versions constrained, redis 8.1.0 confirmed, and pip check passed.
Core-only collection: 772 cases. Focused migration/readiness/source-registry/API/
Redis/audit run: 197 passed, 0 failed, 0 skipped, 1 warning in 41.29 seconds.
Complete isolated Core run: 772 passed, 0 failed, 0 skipped, 1 warning in 75.05 seconds.
The warning is the existing Starlette TestClient/httpx deprecation.

Validation used task-owned PostgreSQL 17.11 on loopback port 15991 with tmpfs and
Redis on port 15990 with RDB/AOF disabled. Database/system and Redis run identities
were independently verified, dotenv disabled, and unexpected network/DNS/database
destinations rejected. Providers were mocked. Before the full run, its empty-Redis
preflight caught one expiring quota key left by the focused run; it was verified as
test-owned and removed before rerunning. That preflight abort collected no tests
and required no application changes.

Cleanup verified zero child databases and removed session fixture tables, with
zero Redis keys remaining. Both task-owned containers and their tmpfs storage were
removed and both test ports closed. Temporary test/dependency resources were
removed; no persistent services or independent application repository were changed.

## Legacy selection retained for the removal milestone

The default pytest.ini still collects the combined repository. These 22 legacy,
consumer-client or mixed modules intentionally remain untouched and outside the
Core-only selection; retire/reclassify them together with the legacy code later:

```text
backend/tests/api/test_global_chess_analysis.py
backend/tests/integration/test_global_chess_database_postgres.py
backend/tests/integration/test_global_chess_migrations.py
backend/tests/integration/test_global_chess_readiness.py
backend/tests/test_argus_core_client.py
backend/tests/test_classification_size.py
backend/tests/test_global_chess_database.py
backend/tests/test_global_chess_discovery.py
backend/tests/test_global_chess_rss.py
backend/tests/test_global_chess_sources.py
backend/tests/test_global_chess_startup.py
backend/tests/test_google_news.py
backend/tests/test_ingestion_characterization.py
backend/tests/test_ollama_relevance.py
backend/tests/test_relevance_accuracy.py
backend/tests/test_relevance_classifier.py
backend/tests/test_relevance_core.py
backend/tests/test_research_agent_core.py
backend/tests/test_research_core_integration.py
backend/tests/test_research_deduplication.py
backend/tests/test_research_relevance.py
backend/tests/test_synthesis_service.py
```

Core's selected tests and fixtures have no executable imports of that application.
The frozen unrelated table names and negative import-guard module names are
intentional. Legacy source modules/settings/migrations, default-suite selection,
historical documentation and consumer-specific dependencies remain for the
separate reviewed removal/dependency-pruning milestone. Do not delete only the
application files while leaving their tests in the default combined selection.
