# Global Chess RSS topic ingestion: current behavior

This characterizes ingestion before replacing network retrieval, including its
request-local duplicate-URL repair. Only deduplication changed in ingestion;
retrieval, scoring, ORM, schema, migration and startup behavior remain unchanged.
The tests retain the original behavior coverage and now prevent the duplicate bug.

## HTTP contract and ownership

Global Chess registers synchronous `POST /ingest` in [main.py](app/main.py).
[ingestion.py](app/routes/ingestion.py) declares no body, query parameters,
authentication dependency, scope or rate limit. A supplied body/query is ignored;
GET returns 405. The Core application does not register this route. Global Chess's
factory currently has no Core request-audit middleware configured.

Normal responses are HTTP 200, including partial or total feed failure:

```json
{"inserted": 2, "updated": 0, "feed_errors": []}
```

Each feed error has `source` and `error`. An empty unparseable feed reports
`Feed could not be parsed`; other exceptions expose `str(exc)[:300]`. There is no
response model, skipped-entry count, partial-coverage header or snapshot metadata.
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

The route calls `feedparser.parse(feed_url)` sequentially, with no explicit timeout,
size bound, concurrency, pagination or retry policy. Each parsed entry is processed
in original order. It does not apply research topic/entity filtering, relevance
classification, publisher diversification or candidate budgets.

- Missing, null, empty or whitespace-only titles/links are silently skipped.
- Titles are stripped, scored in full, then stored at most 500 characters long.
- URL normalization removes common tracking parameters/fragments, lowercases
  scheme/host, and removes trailing path slashes. There is no scheme, public-IP,
  absolute-URL or 2,048-character validation in this ingestion path. Relative,
  FTP and literal-private URLs are stored without being fetched as articles.
- `Topic.source` is the configured feed label, ignoring entry publisher metadata.
- New records get an auto-generated ID, `candidate` status, null summary, parsed
  publication date, scores, and the model's UTC `fetched_at` insertion default.
  Even zero-relevance stories are inserted. Snippets, feed item IDs and discovery
  provenance are not Topic fields and are not persisted.
- Existing rows retain ID, URL, editorial status and summary. Title, source,
  publication date, fetched timestamp and all scores are overwritten. A missing
  incoming date erases an existing date rather than preserving it.

The [Topic model](app/models/topic.py) uses a unique URL, `String(500)` title,
`String(2048)` URL, `String(200)` source and timezone-aware timestamp declarations.
SQLite does not enforce VARCHAR lengths or retain timestamp timezone metadata;
PostgreSQL does. An oversized URL succeeds on SQLite but fails the final commit
on PostgreSQL. This backend difference is covered explicitly.

## Publication dates and scoring

[rss_service.py](app/services/rss_service.py) only re-exports normalization/date
helpers and the Global Chess scorer. Actual date logic lives in
[source_normalization.py](app/utils/source_normalization.py).

Date precedence is truthy `published_parsed`, otherwise raw `published` or raw
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

- Empty `bozo` feeds report an error and processing continues to the next feed.
- `bozo` feeds with entries are processed without a feed error.
- An exception mid-feed aborts its remaining entries, but earlier pending writes
  remain and commit alongside successful later feeds. Invalid title types, URL
  parsing errors and malformed date tuples exhibit this behavior.
- Both feeds failing still returns 200 with two feed errors. Raw exception text
  can expose diagnostics; the current route does not sanitize it beyond length.
- A failure after actual SQL INSERT/UPDATE operations but before commit completion
  rolls everything back and preserves prior database values.

## Core RSS compatibility and migration strategy

Core's [RSS contracts](app/services/discovery/rss_contracts.py) provide approved
feed IDs, immutable snapshots, ordered duplicate observations, UTC dates, explicit
rejections/warnings and pagination. The [existing Global Chess client](app/clients/argus_core_client.py)
collects all pages and carries validated partial sources in `RSSDiscoveryError`.
It can be reused for retrieval, but research's editorial selection function cannot
be reused for ingestion: that would introduce filtering, budgets and deduplication.

1. Keep these characterization and deduplication tests as the baseline. The
   request-local normalized-URL map and distinct-topic counters are implemented;
   preserve them when replacing retrieval. No global autoflush change is needed.
2. Replace only retrieval with the Global Chess-owned authenticated Core client,
   using server-side `discovery:read` credentials and the approved IDs above.
   Preserve configured order/labels, full snapshot traversal and duplicate
   observations; do not add direct-provider fallback or snapshot restart/retries.
3. Adapt full titles and UTC datetime/null values directly into the existing
   scorer and Topic writer. Do not pass Core datetimes through the legacy
   feedparser-dictionary date parser. Keep scoring and editorial state ownership
   in Global Chess, including score-before-title-truncation behavior. Keep Topic
   `fetched_at` as ingestion time; Core's snapshot timestamp describes shared cache
   retrieval and is not the time this Topic was ingested.
4. Agree on strict-parser compatibility changes before implementing: Core rejects
   unsafe/invalid/overlong article URLs, resolves relative links, bounds title and
   snippet fields, and rejects malformed XML instead of feedparser recovery.
   Limits on fields ingestion does not store can still reject an entire entry.
   Core's broader date parsing can change freshness compared with legacy fallback.
5. Preserve usable entries from incomplete snapshots or later-page failures and
   report failed coverage through the existing `feed_errors` shape. Define how
   rejected observations, cursor expiration, 401/403, 429/Retry-After, outages and
   timeouts map into that contract; changing HTTP status/adding response fields
   requires an explicit compatibility decision. Never call missing coverage an
   ordinary empty success.
6. Keep one final database transaction, explicit rollback, unique Topic URLs,
   candidate defaults and existing editorial state preservation. Do not register
   candidates in Core or change schemas. Rerun both database backends and add
   mocked Core pagination/authentication/error tests before enabling retrieval.

At Core's maximum 500 observations and page size 75, ingestion may need 14 Core
requests: two initial discovery operations and 12 continuations, all also charged
to general quota. Snapshot TTL is five minutes and client collection is bounded
to seven pages/60 seconds per feed. These bounds, parser limits and quotas are new
explicit failure cases compared with legacy unbounded feed traversal.

## Isolated verification

[test_ingestion_characterization.py](tests/test_ingestion_characterization.py)
mocks all feed retrieval, freezes scoring/fetch clocks, and overrides the request
session. It uses memory SQLite and optionally disposable PostgreSQL 17. PostgreSQL
requires an explicit test URL and independently verified cluster system identifier;
each case creates/drops only its own random test schema. It never imports the
legacy application's schema-creating startup lifecycle or uses its real session.

No prior dedicated `/ingest` or scoring tests were found; existing research,
analysis and migration tests mainly seed Topic records for other workflows.
