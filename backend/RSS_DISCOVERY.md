# Approved RSS discovery

RSS is an additive branch of `POST /api/v1/discovery`. Google News request and
response models/defaults remain unchanged. Global Chess research and ingestion
are not switched to this provider. No schemas, migrations or credentials change.

## Registry and request

The immutable configuration in `services/discovery/feed_registry.py` approves:

| ID | Label | HTTPS feed |
| --- | --- | --- |
| bbc-world | BBC World | https://feeds.bbci.co.uk/news/world/rss.xml |
| npr-politics | NPR Politics | https://feeds.npr.org/1014/rss.xml |

Each entry includes an enabled flag and configuration revision. Unknown/disabled
IDs return sanitized 422 before storage/networking. Feed selection accepts no URL,
headers, query, origin, or limit overrides beyond the bounded page size:

```http
POST /api/v1/discovery
X-API-Key: <server-side-key-with-discovery:read>
Content-Type: application/json

{"provider":"rss","feed_id":"bbc-world","limit":1}
```

`provider=rss` and one feed ID are required. `limit` defaults to 75, is a strict
integer 1-75, and counts entry observations, including rejected entries. Unknown
request fields are forbidden. Google News still defaults when provider is omitted;
its query is still required. An RSS request does not accept an editorial query.

An illustrative page has this shape (candidate contents shortened here):

```json
{
  "provider":"rss",
  "feed_id":"bbc-world",
  "feed_label":"BBC World",
  "feed_url":"https://feeds.bbci.co.uk/news/world/rss.xml",
  "feed_revision":"1",
  "feed_title":"BBC World fixture",
  "format":"rss2",
  "status":"incomplete",
  "candidates":[],
  "rejected_entries":[{"entry_index":0,"reason":"missing_title"}],
  "snapshot_id":"<snapshot-id>",
  "fetched_at":"2026-11-06T10:00:00Z",
  "expires_at":"2026-11-06T10:05:00Z",
  "total_entries":3,
  "invalid_entries":1,
  "duplicate_observations":0,
  "offset":0,
  "entries_in_page":1,
  "has_more":true,
  "next_cursor":"<opaque-cursor>",
  "error":null,
  "retry_after":null
}
```

Continue using the same feed ID, application identity, and page size:

```json
{"provider":"rss","feed_id":"bbc-world","limit":1,"cursor":"<opaque-cursor>"}
```

Candidates contain provider/source_type `rss`, complete bounded title/snippet,
normalized URL/canonical URL/domain, the configured publisher label, a nullable
UTC date, and provenance: original link, GUID/Atom ID, raw date/date field,
reported publisher/author, warnings, and original zero-based entry index.
Relative article links, including xml:base, are resolved as metadata only and
validated; they never initiate a fetch. XML/HTML snippets remain untrusted content.

## Completeness and pagination

The complete bounded feed is parsed before the first page is published. Entry
order and duplicate observations are retained. `duplicate_observations` counts
repeated normalized URLs among valid observations; it does not remove them.
Candidate and rejection lists can be merged by entry_index to reconstruct each
page. Offsets/page sizes count both kinds. Clients must follow next_cursor until
has_more is false; page limits never silently discard the rest of the snapshot.

Missing title, invalid/unsafe/overlong article URL, and field limits produce an
explicit rejected-entry index and reason. Invalid dates remain null with a warning;
missing dates are ordinary null values. Timezone-less dates are interpreted as UTC
with a warning. Any rejection/date warning makes status `incomplete` on every page.
This is distinct from `has_more`, which reports whether more observations remain.
No fields are silently truncated. Rejected unsafe URLs are not echoed back.

Limits per entry: title 8,192 characters, snippet 65,536, URLs/IDs 2,048, raw date
and reported publisher 512. Limits on the document itself fail the whole operation:
there is no successful truncated feed or partially published snapshot. Malformed
XML is not recovered. RSS 2.0 and Atom are supported; RDF/other roots fail explicitly.

## Bounded Redis storage

Production uses existing Core Redis, never a process-local fallback. There is one
immutable serialized snapshot key per approved feed, maximum 2 MiB each, with an
absolute five-minute TTL. Maximum two slots means at most 4 MiB snapshot payload
plus bounded Redis/key overhead. There are no per-request/application snapshot
keys, retained histories, unbounded indexes, or early eviction by this service.
Live first-page requests reuse the same snapshot without extending its expiry.
Registry expansion beyond two entries fails storage admission until its explicit
slot/memory budget is reviewed. Oversized serialization is rejected, not truncated.

The snapshot stores its feed label, URL and revision as well as observations, so
rolling-worker configuration differences do not change a live page's metadata.
Redis atomic scripts publish only while the caller owns the feed's fetch lease,
refuse to overwrite a live snapshot, and release only the matching lease token.
The lease lasts 30 seconds. Another refresh returns 503/busy with Retry-After 1;
it does not start a second upstream request or spin/wait/retry.

Cursors are HMAC-SHA256 signed with a random per-snapshot secret stored only in
Redis. They bind version, application ID (not API-key ID), feed, snapshot ID,
offset and page size. No deployment signing secret or credentials are introduced.
Malformed/tampered/mismatched cursors return 400. Missing/expired/unknown snapshots
return 410 and require a new first-page request. A cursor never rebases onto a
new snapshot. Cursors are replayable for the same page while the snapshot lives.

The Redis deployment needs capacity for these payloads plus existing quotas and
audit/application needs. External Redis eviction, restart, or operator deletion
can expire paging state early; callers receive 410 rather than inconsistent data.
Use an appropriately configured ephemeral Redis deployment if disk retention is
undesired: this service sets TTLs but does not change Redis AOF/RDB/backup policy.
Worker clocks should be synchronized. No PostgreSQL source/topic records are used.

## Security, quotas, and failures

- Initial requests use only the two configured HTTPS endpoints. Redirects are
  limited to three hops on that feed's exact HTTPS origin, port 443; no downgrade,
  cross-origin, credentials, or fragments. Additional redirect origins require
  explicit registry/security review, not a request option.
- PublicHTTPTransport rejects nonpublic/mixed DNS answers, pins numerical peer
  addresses, and preserves Host/TLS SNI/certificate identity. No inherited proxies,
  cookies, incoming API headers, or credentials are sent to feeds. Query/credential
  logging through HTTPX's high-level client is avoided by using its transport API.
- Network/compressed and decompressed bytes are independently capped at 1 MiB.
  Identity/gzip/zlib-wrapped deflate are supported with stream integrity checks.
  Connect/read/write/pool timeouts are 5/10/10/2 seconds; total fetch time is 20
  seconds across redirects. XML depth is capped at 32, elements at 10,000, entries
  at 500, and cooperative parser time at two seconds. DTDs and entity declarations
  are rejected, external resource resolution is denied, and parsing runs in a
  worker thread without abandoning work on cancellation.
- Two RSS operations run per worker; Redis leases coordinate feed refreshes across
  workers. Total operation time, including capacity waits/storage, is 25 seconds,
  with bounded cancellation-shielded response/lease cleanup. Each Redis command
  has a two-second deadline. Native parsing steps cannot be forcibly preempted;
  byte/chunk/structure bounds constrain them.
- Existing `discovery:read`, general (120/minute), and discovery (10/minute)
  application/key quotas apply to every request, including cursor pages. Live
  snapshot reuse bounds normal upstream polling to one refresh per feed per TTL.
  Upstream 429 establishes a shared cooldown from validated Retry-After, default
  60 seconds and capped at one day. Other fetch/parse errors establish a short
  cooldown (five seconds unless upstream guidance is provided). No automatic retry.

Empty feeds return 200/success; partial valid/rejected observations return
200/incomplete. Unknown feeds return 422, cursor errors 400/410, provider throttling
429, timeouts 504, and malformed/unsafe/oversized/parser/encoding/upstream failures
502. Redis failure returns 503/storage_unavailable, never an in-memory fallback.
Errors are constants and do not expose upstream bodies, exception strings, or
credentials. Authentication/quota failures keep their existing detail envelope;
RSS provider failures use the RSS result envelope and safe Retry-After guidance.

Discovery has no ORM/session or source-registration calls. Existing API-key usage,
audit logging and Redis quota accounting remain. The synthetic BBC/NPR fixtures
and mocked HTTP/DNS tests do not assert today's live provider formatting/redirects.
Global Chess migrations and wider format support are separate future milestones.
