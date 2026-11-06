# Core Google News discovery

`POST /api/v1/discovery` returns source candidates without registering articles,
extracting full text, classifying relevance, or modifying topics. Global Chess's
existing RSS/Google News/GDELT paths are unchanged. No migration is required.

## Request and response

Use a server-side Core API key with the new `discovery:read` scope:

```http
POST /api/v1/discovery
X-API-Key: <server-side-key>
Content-Type: application/json

{"provider":"google_news","query":"geopolitical research topic","limit":10}
```

`provider` defaults to `google_news`; it is the only supported provider. `query`
is stripped, nonempty, at most 1,000 characters, and excludes embedded control
characters. `limit` is a strict integer from 1 to 75. Unknown fields are rejected,
including caller-supplied fetch URLs. Invalid requests produce HTTP 422.

Illustrative successful response (one candidate):

```json
{
  "provider": "google_news",
  "status": "success",
  "candidates": [{
    "title": "Supplied report",
    "url": "https://news.google.com/rss/articles/example",
    "canonical_url": "https://news.google.com/rss/articles/example",
    "domain": "news.google.com",
    "publisher": "Example News",
    "published_at": "2026-11-06T10:00:00Z",
    "snippet": "Only supplied source information.",
    "provider": "google_news",
    "source_type": "google_news",
    "provenance": {
      "provider_item_id": "example",
      "original_url": "https://news.google.com/rss/articles/example?utm_source=rss",
      "published_raw": "Fri, 06 Nov 2026 10:00:00 GMT",
      "publisher_url": "https://example.com/",
      "locale": "US:en",
      "truncated_fields": []
    }
  }],
  "invalid_candidates": 0,
  "duplicates": 0,
  "truncated": false,
  "error": null,
  "retry_after": null
}
```

The fixed upstream request is HTTPS `news.google.com/rss/search` with `q`,
`hl=en-US`, `gl=US`, and `ceid=US:en`. No incoming headers or API credentials are
forwarded. Search queries are not logged by this implementation; it uses HTTPX's
public transport interface rather than the high-level client request logger.

Results keep feed order. URLs use the existing Core normalization utility and are
deduplicated within the response; this does not establish publisher-declared
canonical identity. Google News wrapper links remain wrapper links. No publisher
article is fetched or resolved to unwrap it. Missing/invalid dates are null; valid
RSS publication dates are converted to UTC with the existing utility.

Malformed, credential-bearing, internal/literal-private, or overlong candidate
URLs are discarded and counted. URLs are never truncated. Candidate hostnames
are not DNS-resolved during parsing: every future dereference must independently
apply the public-address transport checks. Optional invalid publisher URLs are
omitted. Titles/publisher names retain the existing 1,000/255-character bounds;
snippets are bounded at 16,384 characters. Text truncation is explicitly listed
in `provenance.truncated_fields`. Snippets may contain untrusted HTML and must be
escaped or sanitized by consumers. `truncated` means additional valid unique
candidates exceeded the requested result limit.

## Failure contract and quotas

Successful empty results are HTTP 200 with `status=success` and an empty list.
Provider failures return the same result envelope with empty candidates:

| HTTP | Provider status | Meaning |
| --- | --- | --- |
| 429 | `throttled` | Upstream Google News returned 429 |
| 504 | `timeout` | Fetch or operation deadline expired |
| 502 | `malformed_feed` | Invalid RSS/XML, content type, length, or compressed stream |
| 502 | `unsafe_destination` | Provider destination or redirect rejected |
| 502 | `response_too_large` | Download/decompressed byte limit exceeded |
| 502 | `parser_limit` | XML byte, depth, node, item, or parsing-time limit exceeded |
| 502 | `unsupported_encoding` | Encoding other than identity/gzip/zlib-wrapped deflate |
| 502 | `upstream_error` | Other upstream status, transport, or unexpected provider failure |

Errors contain constant sanitized messages, never upstream bodies, queries,
exception strings, or credentials. Valid Retry-After seconds/HTTP dates become
nonnegative seconds, capped at 86,400, in the envelope and response header.
There are no automatic retries or sleeps.

Existing authentication failures retain their HTTP 401/403 `detail` contract.
The existing Redis limiter applies the general quota (120/minute) and a new
discovery quota (10/minute), each shared across an application's keys and also
enforced per key. Local quota failures use the existing 429 `detail` contract and
Retry-After header. Redis failure fails closed with the existing 503 contract.
These are separate from upstream provider throttling. No distributed Google News
cooldown/cache or aggregate cross-application provider quota is implemented yet.

## Outbound and parser security

- Only the fixed provider endpoint is constructed from user input; the query is
  encoded as data. Redirects are limited to three hops on HTTPS `news.google.com`
  port 443. Cross-origin and protocol-downgrade redirects fail closed.
- `PublicHTTPTransport` validates every URL, rejects nonpublic/mixed DNS answers,
  pins the numerical connection address, and preserves the Google hostname for
  Host, TLS SNI, and certificate verification. Environment proxies are disabled.
- Raw download and decompressed XML are independently bounded at 1 MiB. Streaming
  reads reject invalid lengths, truncated compressed streams, trailing members,
  and decompression expansion beyond the output limit. Brotli/raw deflate are not
  accepted. Redirect and error bodies are closed without downloading them.
- Connect/read/write/pool phase limits are 5/10/10/2 seconds, with a 20-second
  fetch deadline spanning all redirects. The operation deadline is 25 seconds,
  including capacity waits. Response cleanup is cancellation-shielded and bounded.
- XML uses lxml with entity resolution, DTD loading, external networking, recovery,
  and huge-tree mode disabled. DTD-bearing documents are rejected, including
  alternate XML encodings. Parsing is capped at 10,000 elements, depth 32, and
  500 items. Input is fed in 8 KiB pieces with a cooperative two-second parsing
  deadline. Native parser work cannot be forcibly preempted; byte/chunk/structure
  bounds limit each step. Parsers run off the event loop and are not abandoned
  while continuing to consume resources. Five operations may run per worker.

Only RSS-shaped XML and the documented XML content types are supported initially. Unexpected Google consent
redirects, alternate formats/encodings, or larger feeds fail explicitly rather
than relaxing controls. Existing HTTPX/httpcore/AnyIO pins remain required by the
shared transport; see ARTICLE_FETCH_SECURITY.md. The shared normalizer's existing
trailing-slash/query behavior remains unchanged.

## Persistence boundary and testing

Discovery service/provider code has no ORM/session or source-registry dependency.
It cannot register sources or change Global Chess tables. Existing authentication
updates `core_api_keys.last_used_at`; the Core application also retains request
audit logging and Redis quota accounting. Those security side effects are not
disabled by the read-only discovery operation.

Offline tests use XML fixtures, MockTransport, fake DNS/socket streams, and
in-memory SQLite authentication. They cover malformed/empty feeds, dates,
candidate validation/deduplication, compression bombs/truncation, parser limits,
redirects, DNS rebinding, TLS identity, timeouts, quotas, credentials, and a SQL
write guard permitting only existing API-key usage bookkeeping. No live provider
or persistent database is needed. The fetcher/provider dependency interfaces are
trusted test seams; transport injection is never exposed through the API.
