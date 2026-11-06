# Global Chess topic analysis through Argus-Core

Only `POST /topics/{topic_id}/analyze` has migrated in this milestone:

    Global Chess route -> editorial prompt and schema
        -> CoreInferenceClient -> authenticated HTTP POST /api/v1/ai/generate
        -> Core model backend -> Core response envelope
        -> Global Chess Pydantic/range validation -> TopicAnalysis persistence

The client and its settings belong to Global Chess and import no Core settings,
database models or internal services. Existing packages, tables and editorial
prompts remain in place. `analyze_topic_with_ollama` retains its legacy import name,
but topic analysis never calls its module's direct Ollama client. Classification,
research planning, research evaluation and synthesis retain their existing paths.

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
topic analysis. Prompts are not silently truncated. Other research workflows are
not migrated, and the existing legacy application still needs its own user-facing
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
