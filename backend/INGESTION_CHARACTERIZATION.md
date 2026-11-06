# Global Chess RSS topic ingestion: current behavior

RSS retrieval now uses the existing Global Chess-owned authenticated Core client.
Editorial scoring and Topic persistence remain in Global Chess. Request-local URL
deduplication, distinct counters, the response body and final commit/rollback are
preserved; Core contracts, research, Google News, GDELT and schemas are unchanged.

## HTTP contract and ownership

Global Chess registers asynchronous `POST /ingest` in [main.py](app/main.py).
[ingestion.py](app/routes/ingestion.py) declares no body, query parameters,
caller-authentication dependency, scope or rate limit. A supplied body/query is ignored;
GET returns 405. The Core application does not register this route. Global Chess's
factory currently has no Core request-audit middleware configured.

Normal responses are HTTP 200, including partial or total feed failure:

```json
{"inserted": 2, "updated": 0, "feed_errors": []}
```

Each feed error retains exactly `source` and `error`. Messages are sanitized
constants; upstream bodies and exception strings are never forwarded. Validated
Retry-After guidance is appended to that feed's error string without retries or
sleeping. HTTP 200 remains the response even when both feeds fail, so callers must
inspect `feed_errors`. There is no added response field or partial-coverage header.
Successful empty snapshots produce no feed error.
Commit failure explicitly rolls back and returns HTTP 500:

```json
{"detail": "Database write failed during ingestion"}
```

## Feed traversal and stored fields

[config.py](app/config.py) supplies an ordered mapping:

| Label | Current URL | Approved Core ID |
| --- | --- | --- |
| BBC World | `https://feeds.bbci.co.uk/news/world/rss.xml` | `bbc-world` |
| NPR Politics | `https://feeds.npr.org/1014/rss.xml` | `npr-politics` |

`INGESTION_RSS_FEEDS` in [global_chess_config.py](app/global_chess_config.py)
configures the approved IDs and legacy labels, separately from research. The route
collects complete immutable Core snapshots sequentially in this order, using
`get_core_discovery_client` and server-side `discovery:read` credentials. The
existing base URL, API key and timeout settings apply. Caller-supplied body/query
URLs are ignored. Direct feedparser networking and fallback are removed.

The client validates every page/cursor/offset and immutable snapshot identity,
retaining ordered duplicate observations. Collection is bounded to seven pages,
500 observations, 60 seconds and 4 MiB aggregate response content per feed. The
byte validation runs after HTTP buffering, not as a streaming download cap; Core
separately bounds feed downloads. No snapshot restarts or automatic retries occur.
The client disables environment proxies and redirects to other Core destinations.

Both feed collections finish before database work. A synchronous worker thread
runs the Topic writer using the request session, avoiding SQL work on the event
loop and network waits inside the database transaction. It does not apply research
topic/entity filtering, relevance classification, publisher diversification or
candidate budgets.

- Invalid/missing titles or URLs are rejected and coverage is reported, retaining usable entries.
- Titles are stripped, scored in full, then stored at most 500 characters long.
- URL normalization removes common tracking parameters/fragments, lowercases
  scheme/host, and removes trailing path slashes. There is no scheme, public-IP,
  absolute-URL validation in the legacy route. The migrated client/adapter now
  validates public HTTP(S) URLs and a 2,048-character bound, including normalized
  length before storage. Relative originals are resolved by Core and preserved
  in provenance; relative destination URLs, FTP and literal-private destinations
  are rejected. Candidates are never fetched as articles during ingestion.
- `Topic.source` is the configured feed label, ignoring entry publisher metadata.
- New records get an auto-generated ID, `candidate` status, null summary, parsed
  publication date, scores, and the model's UTC `fetched_at` insertion default.
  Even zero-relevance stories are inserted. Snippets, feed item IDs and discovery
  provenance are preserved in transient ingestion observations, not Topic columns.
  They are never passed as ORM constructor fields and are not persisted.
- Existing rows retain ID, URL, editorial status and summary. Title, source,
  publication date, fetched timestamp and all scores are overwritten. A missing
  incoming date erases an existing date rather than preserving it.

The [Topic model](app/models/topic.py) uses a unique URL, `String(500)` title,
`String(2048)` URL, `String(200)` source and timezone-aware timestamp declarations.
SQLite does not enforce VARCHAR lengths or retain timestamp timezone metadata;
PostgreSQL does. The migrated adapter rejects oversized URLs on both backends
before writes, unlike the legacy SQLite-success/PostgreSQL-500 behavior. It also
rejects NUL in titles; full valid titles are scored before the 500-character cut.

## Publication dates and scoring

[rss_service.py](app/services/rss_service.py) only re-exports normalization/date
helpers and the Global Chess scorer. Actual date logic lives in
[source_normalization.py](app/utils/source_normalization.py).

The migrated route uses Core's timezone-aware UTC datetime/null directly, never
reparsing it through the feedparser dictionary helper. Core can parse ISO dates
that the legacy raw-string fallback could not; this may change freshness where
legacy parsing lost a date. Topic `fetched_at` is local ingestion time, not Core's
shared snapshot timestamp.

For historical comparison, legacy date precedence was truthy `published_parsed`, otherwise raw `published` or raw
`updated`. Parsed publication tuples are interpreted as UTC; RFC date strings
are converted to UTC, with naive timestamps assumed UTC. `updated_parsed` alone
is ignored. An invalid nonempty `published` prevents fallback to `updated`.
Missing/unparseable raw dates return null. Raw ISO timestamps are not accepted by
the fallback parser; feedparser may already have converted them into a supported
`published_parsed` tuple. A malformed truthy publication tuple can raise and abort
the remainder of that feed.

[topic_scoring.py](app/services/topic_scoring.py) applies editorial policy:

- Relevance: casefolded substring matching; add each matching KEYWORDS weight once
  and cap at 100. Repeated phrases do not multiply weight; word boundaries are
  not required (`congressman` matches `congress`).
- Freshness: 40 for a missing date. Otherwise clamp negative age to zero and use
  `max(0, 100 - age_hours / 72 * 100)`. Current/future dates score 100; 36 hours
  scores 50; 72 hours and older score zero.
- Total: `round(0.7 * relevance + 0.3 * freshness, 2)`. Returned relevance and
  freshness are also rounded to two decimal places.

## Transactions, duplicates and failures

[database.py](app/core/database.py) configures `autoflush=False` and
`autocommit=False`. The route queries once for each distinct normalized URL and
caches the Topic object for the rest of the request, including pending inserts.
It adds/edits ORM objects and calls `commit()` once after both feeds. There are no
per-feed commits, explicit flushes, savepoints or per-feed rollbacks. `get_db`
closes the request session; final commit errors explicitly invoke rollback.

**Repaired duplicate-new-URL defect:** previously, pending inserts were invisible
to subsequent SELECTs with autoflush disabled, producing duplicate Topic objects
and a final uniqueness violation (PostgreSQL SQLSTATE 23505). The request-local
URL map now reuses both new and existing objects across entries and feeds.

Counters describe distinct topics, not observations:

- `inserted` counts each distinct URL first added during this request once.
- `updated` counts each distinct URL already found in the database once, even if
  the supplied fields are unchanged. Re-observing a newly added topic does not
  count as an update. Invalid/skipped observations add neither count.
- Last valid observation wins title, source, publication date, fetched timestamp
  and scores. Existing editorial status/summary remain intact; new topics retain
  candidate status and null summary. Feed and entry order remain unchanged.

A subsequent successful ingestion updates a topic inserted by an earlier
completed request. Uniqueness remains enforced in PostgreSQL and SQLite; the map
is request-local, so separate concurrent ingestion requests can still race to
insert the same URL. A database error still rolls back the entire transaction.

Feed failure has different semantics from commit failure:

- Empty success contributes no rows and no error. Incomplete snapshots contribute
  all accepted entries plus a coverage error, including metadata/date warnings.
- Later-page failure/410 expiration retains all prior validated pages; the next
  independent feed is still collected. Invalid cursors and changed snapshots do
  not restart fetching. Malformed HTTP pages fail as a whole, retaining earlier
  pages rather than trusting malformed page contents.
- Authentication/permission failures, 429, 503, timeouts and malformed responses
  produce safe per-feed errors. Retry-After is validated and reported, never slept
  or retried. Both feeds failing still returns 200 with two feed errors.
- Invalid injected-client candidates are rejected individually, preserving other
  valid entries and reporting incomplete coverage. Unexpected writer errors abort
  that feed's remaining entries with a sanitized error, as before.
- A failure after actual SQL INSERT/UPDATE operations but before commit completion
  rolls everything back and preserves prior database values.

## Compatibility and deployment considerations

The [ingestion adapter](app/services/ingestion_discovery.py) reconstructs and
validates Global Chess-owned RSS candidate contracts from the existing client's
source dictionaries. It preserves full titles, URLs, snippets, UTC dates and
provider metadata/provenance, applying no editorial filtering or deduplication.
The existing Topic writer alone owns scoring, label assignment and URL winner
selection. There are no imports of Core implementation services.

Core's [RSS contracts](app/services/discovery/rss_contracts.py) and
[existing client](app/clients/argus_core_client.py) remain unchanged. The server-side
key needs `discovery:read`; credentials never enter browser responses or feed
requests. BBC/NPR identifiers must be approved/enabled and Redis snapshot storage
available on Core. No database migration is required.

Compared with legacy feedparser recovery, strict Core XML parsing can reject an
entire malformed feed. Unsafe/invalid URLs and field limits now generate explicit
coverage errors; limits on unused snippet metadata can still reject an entry.
Valid entries after a rejected entry can survive, whereas a legacy per-entry
exception stopped that feed's remainder. Missing dates remain valid null values;
unparseable date warnings can additionally mark a Core snapshot incomplete.
These are intentional provider/security compatibility changes, not editorial
policy changes. The public body stays `{inserted, updated, feed_errors}`.

Provenance remains transient because Topic has no provenance column. Durable
storage and separate concurrent-ingestion URL races require separate decisions;
the unique URL constraint still protects concurrent writes and can reject a racing
transaction. Legacy application's separate startup schema initialization behavior
is unchanged.

At Core's maximum 500 observations and page size 75, ingestion may need 14 Core
requests: two initial discovery operations and 12 continuations, all also charged
to general quota. Snapshot TTL is five minutes and client collection is bounded
to seven pages/60 seconds per feed. These bounds, parser limits and quotas are new
explicit failure cases compared with legacy unbounded feed traversal.

## Isolated verification

[test_ingestion_characterization.py](tests/test_ingestion_characterization.py)
mocks Core HTTP or the injectable discovery client, forbids direct feed retrieval,
freezes scoring/fetch clocks, and overrides the request session. Tests cover real
wire pagination, partial failures, malformed candidates, deadlines and persistence. It uses memory SQLite and optionally disposable PostgreSQL 17. PostgreSQL
requires an explicit test URL and independently verified cluster system identifier;
each case creates/drops only its own random test schema. It never imports the
legacy application's schema-creating startup lifecycle or uses its real session.

Original characterization tests have been adapted to typed Core inputs; the
unchanged scoring helper and database deduplication/rollback invariants remain covered.
