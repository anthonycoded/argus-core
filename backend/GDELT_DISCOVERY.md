# Core GDELT discovery (disabled by default)

`POST /api/v1/discovery` accepts an explicit `provider: "gdelt"` branch. It requires
the existing Core API key and `discovery:read` scope. Existing general (120/minute)
and discovery (10/minute) application/key quotas and request auditing still apply.
Disabled requests still pass these API protections, but the provider makes no
admission/storage or network calls. RSS continuation quotas are not used by GDELT.

`Settings.gdelt_enabled` defaults to false. Only an operator setting
`GDELT_ENABLED=true` on the Core process enables it; no request field can do so.
No environment/credential files are added or edited. Global Chess research and its
disabled GDELT fallback are unchanged, and its HTTP client is not extended here.

## Contract

```http
POST /api/v1/discovery
X-API-Key: <server-side-key>
Content-Type: application/json

{"provider":"gdelt","query":"Ukraine \"foreign policy\"","limit":25,"window_hours":24}
```

The intentionally small grammar accepts Unicode alphanumeric words and quoted
phrases consisting of such words, with implicit AND between whitespace-separated
units. Quotes must balance. Punctuation, qualifiers (`domain:`), Boolean operators,
negation, wildcards, nesting and escaping are rejected. There are at most 16 units,
32 words, 80 characters per word, 1,000 query characters and 4,096 UTF-8 bytes.
Controls are rejected; ordinary spacing is canonicalized. Raw provider operators
are not passed through. These are resource/syntax policies, not editorial filters.

`limit` defaults to 25 and is a strict integer 1–75. `window_hours` defaults to 24
and is a strict integer 1–168. Unknown fields, endpoints, fetch URLs, headers,
sort/mode/format overrides, enablement, and cursors are rejected with HTTP 422.
The fixed outbound parameters are ArtList, JSON, HybridRel, the validated limit,
and an explicit `timespan=<window_hours>h` at the fixed HTTPS DOC endpoint.

Successful empty response:

```json
{
  "provider":"gdelt","status":"success","limit":25,"window_hours":24,
  "candidates":[],"rejected_entries":[],"received_count":0,
  "invalid_candidates":0,"duplicate_observations":0,"limit_reached":false,
  "fetched_at":"2026-11-06T12:00:00Z","error":null,"retry_after":null
}
```

Before operator enablement, valid authenticated requests instead receive HTTP 503
with `status: "disabled"` and `error: "GDELT discovery is disabled."` in the same
GDELT envelope. Google News's omitted-provider default and RSS contracts are intact.

Candidates retain validated/normalized URL, title, URL-derived domain/publisher,
provider/source type, and provenance. Publication date and snippet are always null:
neither is inferred from a seen timestamp or headline. Known provenance includes
original URL, result index, reported domain, optional provider ID, raw/UTC seen
timestamp, language/source-country attribution, and original/validated mobile and
image URLs. Metadata URLs are never fetched. Text remains untrusted display data.
The reported domain is retained separately rather than trusted as publisher identity.

Order and duplicate observations are preserved; duplicate counts do not remove
records. Invalid records are reported by index/reason. Invalid optional dates or
metadata URLs produce warnings without discarding otherwise useful candidates.
This yields `incomplete`, including when every record was rejected. Missing optional
dates are ordinary null values. Unknown provider fields are not copied into output.
Fields have explicit limits; URLs are never sliced or silently truncated.

`limit_reached` only means the provider returned the requested number of observations,
not that more data definitely exists or that search coverage is exhaustive. Overdelivery
is a parser-limit failure, not silent truncation. There is no pagination, snapshot,
cache or provider-result persistence in this milestone.

| HTTP | Outcome |
| --- | --- |
| 200 | `success` or accepted/rejected/warned `incomplete` |
| 401 / 403 | Existing API authentication/scope detail response |
| 422 | Invalid typed request |
| 429 | Existing application/key quota detail, or GDELT `throttled` envelope |
| 503 | `disabled`, `busy`, `paused`, or `storage_unavailable` |
| 504 | `timeout` |
| 502 | `malformed_response`, `upstream_error`, `unsafe_destination`, `response_too_large`, `parser_limit`, `unsupported_encoding` |

Errors are constants; no upstream bodies, request URLs, queries, credentials or
exception strings are returned or logged by the new provider. Retry-After guidance
is emitted as validated seconds in the envelope/header when applicable. No automatic
HTTP retry, sleeping admission queue, or direct-provider application fallback exists.

## Distributed provider protection

All Core workers/applications sharing the same provider egress must share the same
Redis database. One fixed state hash and one fixed lease key coordinate them:

- `argus:discovery:gdelt:v1:state`: next start, cooldown deadline, optional hold flag.
- `argus:discovery:gdelt:v1:lease`: random token, 40-second expiry.

An atomic Lua admission uses Redis TIME, enforces six-second minimum start spacing,
and permits one in-flight lease. It immediately denies pacing/cooldown or busy
admission, with Retry-After, rather than reserving future slots or queuing. A per-worker
capacity of two also rejects saturation immediately. API quotas are separate from
this global provider gate and cannot substitute for it.

Completion atomically extends cooldown using the maximum of existing protection,
server time plus a validated delay, or a validated absolute HTTP-date deadline.
Only the matching token may release the lease. A stale response may extend provider
protection but cannot release a newer owner's lease. Missing/invalid 429 guidance
uses 300 seconds. Other provider failures reserve six seconds by default. Valid
guidance longer than one day is not shortened; numeric delays are bounded to a
signed-32-bit number of seconds. Extreme valid guidance outside safe arithmetic
bounds sets a fail-closed operator-review hold instead of resuming too early.

The fixed state hash deliberately has no TTL: it contains only bounded scalar
admission fields, never queries, articles, API keys, user IDs or per-query keys.
This preserves cooldown/hold state without an unbounded cache. A hold requires
operator review while the provider is disabled before any deliberate reset; no
public reset API exists. Redis eviction/restart can lose pacing state, so operational
Redis configuration and shared-egress coordination matter. Independent Redis databases
or deployments do not coordinate. The current Core Redis client is not a Redis Cluster
client; cross-slot deployments must be separately supported before use.

Each Redis operation has a two-second deadline. Redis errors, corrupted state or
lost lease ownership fail closed. Failed admission never reaches the network. Provider
completion/cancellation performs bounded shielded lease cleanup; abandoned owners
also expire through the lease TTL. An HTTP request is attempted only once.
The lease outlasts normal operation/cleanup deadlines, but an arbitrary process
pause beyond its expiry cannot guarantee exclusive upstream activity. Redis state
loss and long process pauses require operational monitoring, not automatic retries.

## Fetching and parsing bounds

The fetcher constructs only `https://api.gdeltproject.org/api/v2/doc/doc`. It uses
PublicHTTPTransport directly: validated public DNS answers, numerical-IP pinning,
original Host/SNI and certificate verification, and no inherited proxy/cookie/auth
state. There are no redirects, including same-origin redirects. Only fixed outbound
User-Agent/Accept/Accept-Encoding headers are sent; Core API credentials are not forwarded.

Wire bytes and decompressed output are independently capped at 1 MiB. Identity,
gzip and zlib-wrapped deflate are supported, with length/integrity/trailing-member
checks. Other encodings and non-JSON content types fail explicitly. Streaming raw
chunks prevents unbounded HTTPX decompression allocation. Phase timeouts are
connect/read/write/pool 5/10/10/2 seconds; the fetch deadline is 20 seconds and provider
operation deadline 25 seconds. Cleanup is separately bounded and shielded.

JSON must be UTF-8, an object with an explicit articles array, and no duplicate
object keys or nonfinite numbers. Preflight limits depth to 32, structural/string
tokens to 10,000, string tokens to 65,536 raw characters, numbers to 128 characters, object members
to 64 and records to the requested limit. Candidate field limits apply afterwards.
Parsing runs in a bounded worker thread with a two-second cooperative budget;
threads are not abandoned while parsing. Native JSON steps cannot be forcibly
preempted, so byte/structure limits remain essential alongside deadlines.

Discovery writes no sources or topics. Existing authentication usage bookkeeping,
auditing, application quotas and provider admission state are the only side effects.
No schema or migration changes are required.

## Verification assumptions

The official [DOC API documentation](https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/)
describes phrase searches, article-list/JSON output, hourly timespans and result caps
larger than Core's 75-record policy. No live endpoint calls were made. This historical
documentation does not establish a current pacing guarantee, exact JSON schema,
minimum accepted query, or the meaning of `seendate`. Conventional JSON field names
and compact UTC seen timestamps are covered by synthetic fixtures; seen time is never
labelled publication time. Non-JSON HTTP-200 provider errors are malformed responses,
not heuristically classified as rate limits. Current content-type/error behavior,
field semantics and deployment-wide upstream limits must be verified before enabling.

The six-second start gap, one-flight rule, narrow query grammar, seven-day maximum
window and strict content-type/parser limits are deliberate Core policies. They
may reject otherwise valid upstream features. Tests use mocked transports/DNS and
independently verified disposable Redis, never a live provider or persistent database.
