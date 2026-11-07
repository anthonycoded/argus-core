# Global Chess source-identity client

This milestone adds an application-owned HTTP interface only. No research,
ingestion, agent, or persistence path calls it automatically. No schema, Core API,
scope, quota, or database ownership changes are made.

## Interface and credentials

`CoreSourceClient` exposes `register_source(SourceRegistration)` and
`lookup_source(identifier)`. `ArgusCoreClient` implements it;
`get_core_source_client()` uses the existing server-only
`GLOBAL_CHESS_CORE_BASE_URL`, `GLOBAL_CHESS_CORE_API_KEY`, and
`GLOBAL_CHESS_CORE_REQUEST_TIMEOUT`. No credentials or environment files are added.

Lookup requires `sources:read`. Registration requires **both** `sources:write` and
`sources:read`: after POST `/api/v1/sources/register`, the client verifies the
receipt through GET `/api/v1/sources/{id}`. Existing general/source-write quotas
and auditing apply on Core; verification consumes a second general API request.
Grant scopes explicitly to the server-side key. Never place it in a browser.

```python
from app.clients.argus_core_client import get_core_source_client
from app.clients.source_contracts import SourceRegistration

client = get_core_source_client()
result = await client.register_source(SourceRegistration(
    title="Public article title",
    observed_url="https://example.com/article?utm_source=rss",
    publisher="Observed publisher",
    source_type="rss",
))
# result.observed_url retains the supplied URL independently of
# result.source.canonical_url and result.source.id.
```

The outbound registration body uses Core's existing `title`, `url`, `publisher`,
`published_at`, `source_type`, and optional `content` fields. The Core successful
response envelopes are unchanged. This client does not implement batch submission:
Core batches commit per item and are not a cross-service transaction.

There is currently no exact read-only URL-lookup API. `lookup_source` takes an
already-known ID; `register_source` can create a record and is not a read-only
probe. The client never uses title search or listing to guess an identity.

## Identity and conflicts

The typed result contains the original observed URL/publisher, registration
receipt, verified shared facts, match classification, content-verification state,
and `requires_review`:

| Match | Meaning |
| --- | --- |
| `created` | Core acknowledged a new source; ID, URL and fingerprint were verified |
| `url_matched` | Observed URL normalizes to the existing canonical URL |
| `cross_url_content_matched` | Different canonical URL with a verified full-article fingerprint; `requires_review=true` |

The original duplicate `reason` remains in the receipt. A race can legitimately
return `content_hash` for the same canonical URL; it is classified as a URL match
after fingerprint verification. Publisher/title differences alone do not redefine
identity: observed publisher and Core publisher remain separate. No facts are
updated, no aliases are created, and no records or citations are merged locally.

Known URL/hash disagreement, inconsistent receipt/lookup URLs, a created receipt
with an unexpected fingerprint, and unsubstantiated content matches raise
`CoreSourceError` with local status 409 and an explicit conflict `kind`. This is
an application exception, not a change to Core's HTTP responses. A URL match with
no stored fingerprint reports `content_verification="unavailable"` when article
content was supplied; it never claims the content was verified.

Core IDs are opaque path-safe ASCII segments of 1-36 letters/digits/underscore/
hyphen, beginning with a letter/digit. UUID defaults and ordinary legacy IDs are
supported; paths, query strings, controls and percent escapes are rejected.

Global Chess mirrors the existing v1 HTTP URL normalization and full-article
fingerprint rules in its own contract adapter. Test-only Core pure helpers verify
compatibility. Runtime code imports no Core ORM, database or registry services.
Normalization/hash versioning remains a future Core contract requirement.
Verification detects contradictions in the receipt and returned source only. Core
checks URLs first and exposes no independent hash lookup, so this client cannot
detect a conflicting fingerprint on an unseen record when the matched source has
no stored hash. IDs also remain relative to the configured Core installation;
there is currently no registry-namespace contract.

## Public facts and immutable evidence

Canonical Core identity is a shared reference, not a historical evidence snapshot.
A future Global Chess observation must retain its own observed title, URL,
publisher, dates, exact excerpt, provider provenance and research context. A
canonical lookup must never overwrite that historical evidence. This milestone
adds no observation storage, Core-ID column, foreign key, backfill or research-run
persistence.

`SourceRegistration` forbids unknown fields, including `snippet`, `metadata`,
`notes`, `relevance`, `topic_id`, and `provider_metadata`. There is deliberately no
adapter that accepts a research-source dictionary. Default registration sends no
content and never derives content from snippets or titles.

Optional full article submission requires an explicit `PublicArticleContent` with
`kind="full_public_article"`. Only deliberately supplied, shareable, full article
text belongs there. Text is capped at 250,000 characters / 1 MiB UTF-8; it is hidden
from request repr. Software cannot determine whether a caller falsely labels a
snippet or private text as a full public article, so callers remain responsible
for provenance and permission. Registration must not be enabled automatically as
a substitute for that decision.

The client validates public-looking HTTP(S) URLs without fetching or resolving
them, forbids userinfo/local/literal-private destinations and common credential
query names, and preserves valid original URLs without truncation. This cannot
classify every sensitive query or establish that a hostname resolves publicly.
Source registration itself never dereferences the observed URL. Fields are
bounded, and supplied publication dates require a timezone and normalize to UTC.
Legacy naive timestamps returned by lookup remain naive; no timezone is invented.

Lookup drops Core's arbitrary `metadata` rather than exposing it as application
evidence or forwarding it into another registration. Core's existing shared
catalog/metadata visibility policy is not repaired here. Enable real linking only
after a deliberate public-facts and application-visibility policy is established.

## Failure semantics

All source errors subclass `CoreClientError`. Error strings are constants and
contain no API key, article text, query parameters, upstream body or diagnostic.
Inspect `kind`, `write_outcome`, `retry_after`, and the optional typed `receipt`:

| Write outcome | Meaning |
| --- | --- |
| `not_attempted` | Input/configuration failure, or a standalone read |
| `rejected` | Registration received a definitive 401/403/422/429 rejection |
| `unknown` | Write may have committed: transport loss, timeout, unexpected response, malformed receipt or upstream failure |
| `acknowledged` | Valid registration receipt received; later validation or verification may still fail |

401/403 map to sanitized application-side 502 errors with distinct authentication/
permission kinds. Quotas remain 429, service unavailable 503, timeouts 504, and
standalone lookup 404 remains not-found. Invalid local input is 422. Validated
Retry-After seconds or UTC HTTP dates are retained for 429/503 only.

No method automatically retries, sleeps, queues, restarts or falls back. When a
follow-up lookup fails, the error retains the acknowledged receipt: reconcile by
reading that ID, not by replaying the POST. If the receipt itself was lost, the
client has no trustworthy ID and cannot prove whether creation occurred. It does
not invent an ID or claim rollback. Task cancellation also leaves any attempted
write potentially committed and propagates to the caller. No distributed
transaction, durable retry queue or exactly-once guarantee is provided.

## HTTP isolation and tests

Owned HTTP clients disable environment proxies. Source methods build explicit
requests with server-side X-API-Key; they do not inherit injected client default
cookies, authorization or HTTP basic auth. Redirects are disabled. Inject a trusted
`httpx.AsyncClient(transport=httpx.MockTransport(handler))` for offline use; ownership
stays with the caller. Custom transport retries/hooks remain the injector's
responsibility.

Responses stream raw bytes with a 64 KiB limit, exact declared-length checks,
UTF-8/JSON validation and duplicate-key rejection. The client requests identity
encoding and rejects compressed responses; deployments must honor that negotiation.
Each HTTP request has the configured phase timeout and a total deadline capped
at 30 seconds, with bounded response cleanup. Registration can make two requests.
Malformed/oversized historical metadata can therefore prevent verification even
though it is not exported. Trusted injected transports may return prebuffered
responses, which are size-checked before parsing.

`test_global_chess_sources.py` uses mocked HTTP and pure wire-contract oracles.
It covers identities, conflicts, publisher attribution, privacy restrictions,
protocol bounds, failures, acknowledged receipts and committed-but-lost responses.
Existing discovery/inference/research tests remain separate compatibility checks.
