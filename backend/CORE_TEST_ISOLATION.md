# Core test isolation after legacy application removal

Phase 6I.1 isolated Core tests; Phase 6I.2 removes the reviewed legacy application
files. Core-owned runtime modules, API contracts and Core migration files are unchanged.

Run the default selection with `python -m pytest` or the explicit profile with
`python -m pytest -c pytest-core.ini`. Both contain Core tests only. The conftest
import guard rejects removed consumer models, databases, settings, routes, agents,
clients and editorial services during collection and execution. No consumer
configuration is required. Core assertions rescued from mixed modules are tested
directly; the full Core OpenAPI is compared with its pre-removal snapshot.

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

Producer/client integration tests exercising legacy Global Chess were retired.
Their retirement and retained Core assertions are recorded in GLOBAL_CHESS_REMOVAL.json.
Negative import guards may name legacy modules; frozen sentinel table names are
historical fixtures, not application dependencies.

## Dependencies and safe validation

`redis==8.1.0` matches the already-installed and previously exercised Python
client. Core uses redis.asyncio and redis.exceptions in its connection, rate-limit,
AI admission, GDELT admission and RSS snapshot implementations. Phase 6I.2 removes
only feedparser/feedparser-sgmllib, which have no remaining Core imports or required
dependency consumers. No retained version is upgraded.

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

## Legacy selection retired in Phase 6I.2

These 22 legacy, consumer-client or mixed modules were retired. Their complete
function inventory, ownership rationale and retained Core assertions are recorded
in GLOBAL_CHESS_REMOVAL.json:

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

Core's tests and fixtures have no executable imports of that application. Frozen
unrelated table names and negative import-guard module names are intentional.
Global Chess-specific documentation files now link to GLOBAL_CHESS_SEPARATION.md;
Core security/provider/source/database documentation remains maintained here.

## Phase 6I.2 validation

Pre-removal Core profile: 772 passed, 0 failed, 0 skipped, 1 existing warning in
98.88 seconds. Post-removal default suite in a fresh constrained environment:
778 passed, 0 failed, 0 skipped, 1 existing warning in 68.53 seconds. The warning
is the existing Starlette TestClient/httpx deprecation. Both default and explicit
Core profiles collect the same 778 node IDs. All original Core test functions
remain; six direct contract/foreign-version-history cases preserve assertions
rescued from retired consumer tests.

Full Core OpenAPI (9 paths) exactly equals the pre-removal snapshot in
`tests/fixtures/core_openapi.json`; SHA-256 is
91531845673770d801f70b00f633f6fb4a437f489fcf80f312142d5924c15c89.
Preserved runtime/shared-utility/Core-migration hashes match the pre-removal commit.
Core migrations still have one root/head and initialize only six Core tables.
The enhanced isolation tests preserve unrelated schema/data/version rows in both
schema orders and across Core downgrade. Startup/readiness and cleanup regressions
pass with no Global Chess settings or Python implementation.

Fresh requirements installation omits feedparser and sgmllib entirely, retains
redis 8.1.0 and every other installed version from the constrained baseline, and
passes pip check. No persistent database or live provider was used. Validation
used independently verified task-owned PostgreSQL/Redis with temporary storage,
disabled Redis persistence, and network/DNS/database guards. The independent
Global Chess HEAD and clean working tree were verified unchanged.

GLOBAL_CHESS_REMOVAL.json records all removed/modified/created paths and every
retired test function with ownership rationale. Core source files, migration
history, provider enablement, authentication and source-registration behavior are
unchanged. No Git commit, push or merge is performed.


Cleanup verified zero child databases and no session fixture tables, with Redis
empty. Both task-owned containers/tmpfs stores were removed, both test ports closed,
and the fresh virtual environment plus temporary runner/plans/logs were removed.
Changed-file and captured-output scans found only synthetic negative-test URI
parameters in collection output, with no real secrets. Tracked/untracked diff
checks passed; the independent Global Chess repository remains unchanged.
