# Argus-Core

Reusable intelligence backend for independent applications such as Argus and
Global Chess. Applications integrate through authenticated HTTP; no consumer
application package or database settings are required to run Core.

Core provides FastAPI APIs, application/API-key scopes, Redis quotas/admission,
audit logging, canonical source registration/catalog policy/admission storage,
secure article fetching/extraction, bounded AI inference and approved discovery.
Google News and RSS behavior is unchanged. GDELT remains disabled by default;
this milestone enables neither it nor automatic consumer source registration.

## Run Core

Use Python 3.12 and install backend/requirements.txt in an isolated virtual environment.
Provide Core's DATABASE_URL, REDIS_URL and model/provider configuration securely.
Do not reuse application credentials or expose API keys to browsers/logs. No
Global Chess configuration is needed. Existing environment files are not changed.

Provision -> run reviewed Core Alembic migrations -> start Core:

```text
python -m alembic -c alembic.ini upgrade head
python -m uvicorn --app-dir backend app.core_main:app
```

These are operator deployment instructions, not automatic startup actions. Use the
intended database and least-privilege runtime role; never run test commands against
persistent development/production resources. Read-only readiness gates startup and
shutdown closes Core-owned resources. See backend/DATABASE_STARTUP.md.

## Tests

```text
python -m pytest
python -m pytest -c pytest-core.ini
```

Both selections are Core-only and reject legacy application imports. Supply only
independently verified disposable PostgreSQL/Redis resources, disable dotenv, mock
providers and verify cleanup as described in backend/CORE_TEST_ISOLATION.md.
No live providers or persistent database are required for this validation.

## Ownership and documentation

Global Chess owns editorial research, topic persistence, evidence/citations and
future content workflows in its independent private repository. Argus will likewise
remain a separate consumer. Shared normalization helpers stay Core-owned here;
no shared Python framework/package is introduced.

See backend/GLOBAL_CHESS_SEPARATION.md and backend/GLOBAL_CHESS_REMOVAL.json for the
reviewed extraction/removal and preserved Core assertions. Removing source files
does not drop any historical database tables or alter Core migration history.

Core documentation remains in backend/: ARTICLE_FETCH_SECURITY.md, DISCOVERY_API.md,
RSS_DISCOVERY.md, GDELT_DISCOVERY.md, AI_CAPACITY.md, AUDIT_LOGGING.md,
DATABASE_STARTUP.md, PUBLIC_SOURCE_CATALOG_POLICY.md and SOURCE_ADMISSION_INFRASTRUCTURE.md.
