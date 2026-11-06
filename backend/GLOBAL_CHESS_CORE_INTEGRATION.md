# Global Chess topic analysis through Argus-Core

`POST /topics/{topic_id}/analyze` and the research agent's planning, evaluation,
and synthesis inference now use authenticated Core HTTP:

    Global Chess route -> editorial prompt and schema
        -> CoreInferenceClient -> authenticated HTTP POST /api/v1/ai/generate
        -> Core model backend -> Core response envelope
        -> Global Chess Pydantic/range validation -> TopicAnalysis persistence

The client and its settings belong to Global Chess and import no Core settings,
database models or internal services. Existing packages, tables and editorial
prompts remain in place. `analyze_topic_with_ollama` retains its legacy import name,
but topic analysis never calls its module's direct Ollama client. Classification,
GDELT discovery and ingestion retain their existing paths. RSS and Google News research
discovery use Core HTTP as described below. Research inference uses
`services/research_inference.py` for input-limit and Pydantic validation.

## Server-side configuration

Configure the Global Chess process independently using environment variables (or
the existing backend/.env loader, without committing secret values):

| Variable | Default | Meaning |
| --- | --- | --- |
| GLOBAL_CHESS_CORE_BASE_URL | unset | Core origin and optional deployment prefix, without /api/v1 |
| GLOBAL_CHESS_CORE_API_KEY | unset | Server-side key belonging to the Global Chess application |
| GLOBAL_CHESS_CORE_REQUEST_TIMEOUT | 190 seconds | HTTPX request timeout per network phase, positive and at most 600 seconds |

Register/issue credentials through the existing trusted Core administration
workflow. The key needs `ai:generate` for inference and `discovery:read` for Google
News discovery. Never put it in a browser, URL, request body,
tracked configuration, or access logs. SecretStr masks settings representations.
Use TLS or an appropriately protected private network between deployed services.
Do not point the client at Global Chess itself. Core and Global Chess must run at
distinct addresses/ports. Missing configuration produces 503 on topic analysis;
there is no default destination and no direct-Ollama fallback.

The API key is sent only in X-API-Key. The default client ignores environment
proxies, closes its HTTP client after each operation, and never follows redirects.
An injected AsyncClient is caller-owned and is not closed by the adapter.

## Compatibility

The editorial system/user prompts, temperature 0.2, response schema, and 180-second
Core inference timeout are preserved. The independent 190-second HTTP timeout
allows Core to report its inference timeout; it is not a total job deadline.
Deployment proxies and callers need compatible timeout budgets.

Core's existing response is `{status, model, result}`, where result is a JSON
string. The adapter produces an InferenceResult containing content and actual
model, then Global Chess applies StoryAnalysisResult validation and its existing
0-100 score checks. The public topic-analysis response remains
`{topic_id, title, analysis, model}`. Model is stored and returned from Core's
response, never inferred from local OLLAMA_MODEL. Missing/blank/non-string models,
or models longer than the existing varchar(100) column, fail with 502 before a
database write. Other response fields and insert/update/rollback behavior remain.

## Failure policy: one request, no retries

| Core/transport outcome | Global Chess API response |
| --- | --- |
| 401 | 502, sanitized Core authentication failure |
| 403 | 502, sanitized Core permission failure |
| 429 | 429, rate limit exceeded |
| 503 | 503, inference unavailable |
| Core 504 or client timeout | 504, inference timed out |
| Connection/protocol failure | 502, sanitized request failure |
| Other non-200, including redirects and 422 | 502, rejected inference request |
| Malformed envelope/content or invalid analysis scores | 502 |
| Missing/invalid integration configuration | 503, no request sent |

Upstream authentication errors are integration errors, not browser-login
challenges. The client never forwards upstream bodies or diagnostic exception
strings. For 429/503, valid Retry-After seconds or HTTP dates are forwarded;
invalid values are omitted. The adapter does not sleep or retry. Callers must
honor Retry-After when deciding to submit another operation. There is no
idempotency guarantee for generation, so automatic retries could duplicate work.

Core's existing prompt-size limits (10,000 system / 30,000 user characters),
180-second maximum inference timeout, and application/key quotas now apply to
topic analysis and research inference. Prompts are not silently truncated. The
existing legacy application still needs its own user-facing
authentication/access controls. This milestone adds service authentication only.

## Offline testing

Tests inject the CoreInferenceClient protocol or an HTTPX MockTransport. Contract
tests additionally call the real Core AI router with an ASGITransport, real API-key
authentication against in-memory SQLite, mocked rate-limit dependencies and a
mocked model backend. No live Ollama, Redis, PostgreSQL or external provider is
needed. The Global Chess route is tested with its own in-memory database, without
running either production application lifespan or schema migrations.

Inject the client through get_core_inference_client for route tests or through
analyze_topic_with_ollama(inference_client=...) for service tests. Only this
integration's new settings are independent; the pre-existing Global Chess database
and other research configuration remain coupled until later separation milestones.

## Research inference compatibility

`generate_research_plan`, `evaluate_research_progress`, `synthesize_research`, and
`run_research_agent` accept an optional keyword-only `inference_client`. Default
calls use the same server-side settings and authenticated adapter as topic analysis.
There is no direct-Ollama fallback. GDELT discovery, ingestion, and relevance
classification retain their existing provider paths. RSS and Google News research use Core.

Planning keeps temperature 0.2 and a 120-second inference timeout; evaluation keeps
temperature 0 and 120 seconds; synthesis keeps temperature 0 and 180 seconds.
Editorial prompts, schemas, citation checks, iteration limits, query deduplication,
stopping decisions, and uncertainty reporting remain unchanged. Publication dates
represented by Python date/datetime objects serialize as ISO strings for synthesis;
source dictionaries are not mutated. Existing 25-source and 1,500-character snippet
caps remain in place.

**Input-size incompatibility:** Core accepts at most 10,000 system characters and
30,000 user characters. The previous synthesis allowance alone permits 37,500
snippet characters, excluding metadata; evaluation input and topic/question lengths
also have no overall bound. The research adapter checks the complete rendered prompt
before inference and raises a sanitized `CoreClientError(422)` if it cannot fit.
No evidence is silently removed and no Core limit is increased. Larger jobs require
an explicitly designed chunking strategy or a separately reviewed Core contract
change. Tests compare these local guards against the real Core request schema.

Core failures retain the adapter's status mapping and validated Retry-After value.
Planning/evaluation propagate `CoreClientError` to callers, halting the agent before
further work. Synthesis retains the existing partial-result behavior: sources remain
available, status becomes incomplete, and limitations include the sanitized error
and Retry-After guidance when present. No operation automatically retries or sleeps.
Malformed structured output still fails Pydantic validation; invented citations
still fail source-URL validation. Research functions do not write database records,
and application routes and persistence are unchanged.

The HTTP adapter validates and returns Core's actual model. ResearchPlan,
ResearchDecision, ResearchSynthesis, and ResearchAgentResult currently have no model
field, so this milestone does not add model metadata to those public contracts or
invent provenance from local settings. Topic-analysis provenance remains unchanged.
Durable per-stage research provenance needs a future contract/storage decision.

An agent job consumes one planning request, up to `max_rounds` evaluation requests,
and one synthesis request when sources exist (normally up to five calls at three
rounds), in addition to unchanged discovery/classification activity. Core quotas,
proxy timeouts, and the caller's overall job budget must accommodate this sequence.

## Google News research discovery

The Global Chess client now exposes a separate `CoreDiscoveryClient` protocol and
`discover_google_news(query=..., limit=...)` method. It sends one authenticated
`POST /api/v1/discovery` using the existing Core base URL, server-side API key,
and `GLOBAL_CHESS_CORE_REQUEST_TIMEOUT`. No settings/credentials or Core API contracts
change. Environment proxies and redirects are disabled; injected HTTP clients stay
caller-owned. There is no direct Google News fallback and no automatic retry/sleep.

`clients/discovery_contracts.py` is Global Chess's independent wire adapter; it
imports no Core services, models, or request schemas. It validates provider/status,
the complete candidate batch and provenance, counters, and response limits. Core
successfully returning zero candidates produces `[]`. HTTP errors, malformed JSON,
malformed candidates, and a success response carrying an error are explicit failures.
Invalid batches fail as a whole rather than silently discarding candidates. Unknown
additional response fields are tolerated. The request matches Core's nonblank query
limit of 1,000 characters and strict integer limit of 1-75; incompatible input fails
locally with sanitized 422 without a Core request or query truncation.

The adapter preserves title, URL, publisher, snippet, and `source_type=google_news`.
Publication timestamps become timezone-aware UTC datetimes; null dates stay null,
and invalid/naive dates are rejected. URLs are validated without rewriting/truncation
or fetching. Canonical URL, domain, provider identity, and complete known provenance
are retained in transient `provider_metadata`. Google wrapper URLs stay wrapped.
Existing normalization, processed-URL deduplication, relevance classification,
partial-match review entries, sorting, and publisher diversification remain in
Global Chess. RSS is collected/classified first, then Google News, each with the
existing `min(limit * 3, 75)` candidate budget. GDELT remains disabled in orchestration.

`discover_google_news_sources(..., discovery_client=...)` returns application
dictionaries. `discover_sources` and `run_research_agent` also accept this optional
keyword-only dependency. Route tests can override `get_core_discovery_client`.
The former live Google News tests now use mocked Core HTTP and offline fixtures.
Contract tests additionally use Core's actual router/authentication in process,
in-memory SQLite and a mocked provider fetcher, never a live news/model provider.

### Failure and partial-result policy

Core 401/403 become sanitized 502 integration failures. Core 429/503 retain those
statuses; Core/client timeouts become 504; other rejected requests, malformed
responses, and provider failures become 502. Valid Retry-After seconds/HTTP dates
are retained on errors; invalid guidance is discarded. No upstream diagnostic
body/exception text is forwarded or logged.

If Google News fails after RSS selection, `ResearchDiscoveryError` carries the
selected RSS sources. The research route preserves the existing body and persistence
behavior when partial sources or review entries exist: it saves accepted sources
and returns HTTP 200,
with `X-Research-Partial: true`, `X-Research-Discovery-Status: <mapped error status>`,
and validated `Retry-After` when present. Consumers must inspect these headers to
detect incomplete provider coverage. With neither accepted partial sources nor review entries, the route
returns the mapped error using the existing `{detail}` shape and makes no research
record writes. Successful empty discovery remains a normal success, without partial
headers. This deliberately replaces the old silent failure-as-empty behavior.

The agent retains prior-round evidence and partial RSS sources, marks research
incomplete, and reports the sanitized reason/Retry-After in limitations. It stops
further discovery, evaluation, and synthesis requests after the failure; it does not
retry the failing query or silently present partial coverage as completed research.
Other stopping/iteration/editorial rules are unchanged on successful discovery.

Research persistence explicitly selects the original six source fields before
constructing an ORM record, so metadata cannot become unsupported model arguments.
Existing title/publisher/date updates, insertion, commit/rollback, approval checks,
and public source-summary fields are preserved. The current table has no provenance
column: metadata survives in transient sources/review entries and agent results,
but is not persisted or added to stored-source summaries. Durable provenance needs
a separately approved contract/storage change. Snippets remain untrusted content.

Core's separate 10/minute discovery quota, general quota, secure-fetch/parser limits,
and locale restrictions now apply. The existing 190-second default HTTP phase timeout
is shared with inference; Core currently bounds discovery operations at 25 seconds.
Callers/proxies still need suitable total job budgets. See DISCOVERY_API.md for Core
limits. Relevance classification retains its existing provider behavior.

## RSS research discovery

Research now uses `CoreDiscoveryClient.discover_rss(feed_id=...)`, authenticated with
the existing server-side key, which must have `discovery:read`. It never parses or
fetches RSS locally and has no direct-provider fallback. Ingestion is unchanged.
`global_chess_config.RESEARCH_RSS_FEEDS` maps BBC World to `bbc-world` and NPR
Politics to `npr-politics`, in that order. Ingestion's separate URL configuration
is untouched. These identifiers must remain approved/enabled on the Core server.

The Global Chess-owned `clients/rss_discovery_contracts.py` validates the wire
response independently of Core internals. Each request uses provider `rss`, one
feed ID, limit 75, and an optional opaque cursor. For each feed, the client collects
one immutable snapshot before editorial filtering: at most seven pages and 500
observations, including rejected entries, with a 60-second total deadline and a
4 MiB aggregate response-content validation limit. The HTTP phase timeout setting
still applies; the total deadline can cut it short. HTTP response buffering is
provided by the existing trusted-Core client; the aggregate limit is checked before
JSON parsing, not a streaming download limit. Core separately bounds RSS downloads.

Snapshot identity/metadata, offsets, exact observation indices, page sizes, totals,
rejection counts and cursor progression must agree. Cursors remain opaque: Core
verifies their HMAC and application binding. Global Chess checks cursor presence,
length, repeated tokens, snapshot continuity and page bounds. No automatic retry,
sleep or snapshot restart occurs, including after 410 expiration or 429 throttling.
Malformed pages fail as a whole; only evidence from validated pages is retained.
Successful empty snapshots remain normal empty results. A Core `incomplete`
snapshot is an explicit coverage failure even when all usable entries were fetched.

Entry order and duplicate observations survive collection. Core provenance,
original URLs (including relative originals), publisher attribution, UTC dates,
snippets, feed identity, snapshot metadata and coverage counters remain in transient
source metadata. Application publisher labels stay BBC World/NPR Politics. Existing
topic matching, overlap ranking, first-wins ties, higher-overlap duplicate winners,
1,000-character selected titles, candidate budgets, relevance classification, review
queues, processed-URL deduplication and publisher diversification remain in Global
Chess. The budget is applied after collection, never as a pagination cutoff.

After a feed failure, valid earlier pages still enter editorial selection. The other
configured feed and then Google News are independent operations and may contribute
additional evidence. Research still classifies RSS first. Coverage errors name failed
feeds; if several operations fail, their sanitized reasons are combined and the first
failure determines status/Retry-After. Guidance is validated and returned without
automatic retries. Errors use the existing partial-research headers/persistence path
and incomplete-agent behavior described above. There are no database/schema changes.

Deployment must budget up to 14 RSS discovery requests plus Google News per query,
charged as 15 general, three initial-discovery and 12 continuation operations.
Core retains its 10/minute discovery quota for fetch-capable requests and applies
a separate 60/minute quota to RSS cursor pages, with the general 120/minute quota
on all requests. One maximum-size query therefore fits without raising provider
fetch limits. Concurrent queries and other traffic can still exhaust shared quotas.
Snapshot TTL is five minutes; concurrency, quotas or delays may cause explicit expiration. Coordinate
application/key quotas and job/proxy timeouts operationally; this change neither
weakens quotas nor silently drops evidence to fit them. Metadata remains transient
because the existing research table has no provenance column.
