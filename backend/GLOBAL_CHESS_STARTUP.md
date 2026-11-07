# Global Chess application startup ownership — Phase 6C

`app.main` composes Global Chess through `app.global_chess_factory.create_app`.
The application no longer imports Core's factory, settings, database infrastructure
or initialization hooks. Core continues using its unchanged `app.factory`,
`app.core.config`, `app.core_main`, and read-only database readiness implementation.

The default application title remains `CivicStudio AI - Trend Scout`, version
`0.1.0`. All existing routers, paths, response schemas, operation IDs, documentation
paths, health responses and application middleware behavior are preserved. Global
Chess does not acquire Core audit middleware, authentication, CORS or new routes.
Its factory accepts its own router list and startup/shutdown callbacks and has no
implicit database initialization when those callbacks are omitted.

## Application-owned configuration

`GlobalChessSettings` now owns application metadata, approved RSS feed selections,
editorial keywords/weights, database configuration and Core HTTP integration.
Configuration comes from its existing application settings source; no Core settings
object is imported. This milestone does not edit or inspect environment files.

| Setting | Default / compatibility |
| --- | --- |
| `GLOBAL_CHESS_APP_NAME` | Existing CivicStudio title; generic `APP_NAME` does not select Global Chess's name |
| `GLOBAL_CHESS_APP_VERSION` | `0.1.0`; generic `APP_VERSION` remains a lower-priority compatibility alias |
| `GLOBAL_CHESS_RESEARCH_RSS_FEEDS` | BBC World/bbc-world, then NPR Politics/npr-politics |
| `GLOBAL_CHESS_INGESTION_RSS_FEEDS` | Same ordered defaults, independently configurable |
| `GLOBAL_CHESS_EDITORIAL_KEYWORDS` | Existing 20 phrase/weight entries, unchanged |
| `GLOBAL_CHESS_RELEVANCE_WEIGHT` / `GLOBAL_CHESS_FRESHNESS_WEIGHT` | 0.7 / 0.3; nonnegative finite weights summing to one |
| `GLOBAL_CHESS_FRESHNESS_WINDOW_HOURS` | 72 |
| `GLOBAL_CHESS_MISSING_DATE_FRESHNESS` | 40 |
| `GLOBAL_CHESS_DATABASE_URL` | Phase 6B behavior unchanged; see database ownership documentation |
| `GLOBAL_CHESS_CORE_BASE_URL`, `GLOBAL_CHESS_CORE_API_KEY`, `GLOBAL_CHESS_CORE_REQUEST_TIMEOUT` | Existing authenticated Core HTTP integration unchanged |

Collection settings use BaseSettings JSON decoding; they are server-controlled
configuration, never request-body policy. Feed selections have 1–2 unique approved
IDs, bounded labels and preserved configured order. Keywords have bounded phrases
and integer weights. Unknown IDs, invalid weights and inconsistent scoring profiles
fail configuration validation. Neither feed selection nor compatibility URLs add
caller-supplied network destinations: Core still owns the approved fetch registry.
Changing a policy setting deliberately changes application policy; defaults are
identical to the pre-6C behavior. Route/persistence logic is not changed.

`app.config` remains a compatibility facade exporting `FEEDS`, `KEYWORDS`,
`OLLAMA_BASE_URL`, `OLLAMA_MODEL`, and `settings` (now Global Chess's object).
FEEDS remains an ordered label-to-legacy-URL dictionary; the migrated ingestion
path continues using approved feed IDs, never those URLs for local fetching.
Research/ingestion feed constants keep their tuple-of-pairs shapes. No compatibility
facade exposes Core Redis/database or admission settings.

## Unmigrated relevance classification

Relevance classification remains direct Ollama inference in this phase. The old
shared provider client imports Core settings at module import, so Global Chess now
uses a small temporary application-owned compatibility client in
`clients/global_chess_ollama_client.py`. Its `/api/chat` payload, timeout, schema,
temperature, response parsing, proxy behavior and exception flow match the previous
client. Editorial prompts, classification logic and failure/None semantics remain
unchanged. Topic analysis and research planning/evaluation/synthesis continue using
authenticated Core HTTP as before. Core's generic Ollama client is untouched.

`GLOBAL_CHESS_OLLAMA_BASE_URL` and `GLOBAL_CHESS_OLLAMA_MODEL` select the temporary
classifier destination/model; generic `OLLAMA_BASE_URL` and `OLLAMA_MODEL` remain
lower-priority aliases. Defaults remain `http://localhost:12400` and `qwen2.5:3b`.
No provider destination is changed automatically. Provider URLs are secret-masked
in settings, while existing `app.config.OLLAMA_BASE_URL` remains a string alias.
Do not log that alias. Core API keys/database URLs remain secret-masked and server-
side. Future relevance migration must resolve Core inference quota compatibility;
this phase does not enable a Core fallback, retries or provider changes.

## Lifecycle and remaining limits

Startup still invokes Global Chess's existing five-table `create_all()` callback.
**This remains temporary until Phase 6E.** No migration is run or namespace/source
identity provisioned. Shutdown invokes only Global Chess's existing owned-engine
disposal callback, including on startup failure. Core's startup and shutdown are
not called by this application. Phase 6B database destination selection, development
fallback, session ownership, commits, rollback and status/summary preservation are
unchanged. No data cutover or persistent schema operation is authorized here.

Frozen pre-6C OpenAPI and pre-6B DDL fixtures protect API/schema compatibility.
Tests block the entire Core package during fresh Global Chess startup, block Global
Chess settings/factory during Core composition, verify owned cleanup and provider
wire behavior, and run existing API/persistence regressions with isolated resources.
Validation supplies process settings, disables dotenv reads, and never calls live
providers or development/production databases.

Independent migrations/readiness, remaining shared normalization utilities, test
extraction, packaging/deployment and application access protection remain future
work. The legacy direct GDELT helper remains unused and GDELT stays disabled.
Automatic source registration remains disabled. No authentication/API contract,
schema, credential, environment file, repository layout or persistent data changes
are part of this phase.
