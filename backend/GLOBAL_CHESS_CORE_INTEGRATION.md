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
discovery and ingestion retain their existing paths. Research inference uses
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
workflow. The key needs `ai:generate`. Never put it in a browser, URL, request body,
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
There is no direct-Ollama fallback. Discovery, ingestion, and relevance classification
are unchanged; a full research job still uses their existing provider paths.

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
