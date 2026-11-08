# Global Chess separation

Global Chess is an independent private application repository at the sibling local
project global-chess. Argus and Global Chess are independent consumers of Argus-Core.
Neither application is a Python-package dependency of Core. Use app.core_main:app
for Core; the former app.main entry point is intentionally removed.

Core owns reusable intelligence infrastructure: authenticated APIs, application/key
permissions, quotas, audit logs, canonical sources/catalog admission, secure article
fetch/extraction, bounded AI inference, approved RSS/Google News discovery, and the
default-disabled GDELT provider. Core owns six tables and its existing Alembic chain.
No Core contract, setting, schema or revision changes in this removal.

Global Chess owns editorial topic/research/relevance policy, evidence/citations,
consumer HTTP adaptation, its five application tables, its database configuration,
factory/readiness/migrations, and future content-production/publishing workflows.
Its settings, prompts, endpoints and deployment instructions belong in that repository.
Applications call Core over authenticated HTTP; no cross-database foreign keys or
shared Python ORM imports are introduced. Keys stay server-side and least-privilege.

The independent repository was verified present/clean at
ed9df1a60edb7c06890d03385140858e1449af62 and was not modified. Its extraction manifest
records source 694facddf960a093fdc2ba3e79debaaebdc0680a. No remote URL or credential is
copied here. User-reported independent deployment/integration results are context;
this removal validates Core locally with disposable PostgreSQL/Redis and mocked providers.

No application data or live database was migrated or deleted. Removing legacy Python
files and its application migration directory does not drop any existing tables.
Historical Core revision IDs and Core migrations stay unchanged. Any old combined
local database is outside this task; operators must review it separately.

## Removal inventory and tests

GLOBAL_CHESS_REMOVAL.json contains every removed source path/hash and all 22 retired
module/function inventories with reasons. It also maps the retained Core assertions.
215 top-level legacy/mixed test functions leave this repository; parametrized case
counts differ. Consumer-owned tests are not Core regressions. Core-owned assertions
from those modules are retained or expressed directly against Core contracts,
including both schema orders and unrelated version-history preservation.

The default pytest suite now contains Core tests only and rejects legacy imports.
pytest-core.ini remains an explicit matching profile. Frozen unrelated_application_schema
is a test-owned historical sentinel; its table names are intentionally retained.
See CORE_TEST_ISOLATION.md for isolation rules and validated counts.

Global Chess-specific document filenames remain short references here to preserve
existing links. Their implementation/deployment content belongs in the independent
repository; the source Git history also retains the original documents.
