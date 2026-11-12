# Article fetch security and compatibility

Supported and tested together: **HTTPX 0.28.1, httpcore 1.0.9, AnyIO 4.15.1**.
`requirements.txt` constrains these to exact versions. Python 3.11+ is required
for the total download deadline (`asyncio.timeout`). No other version combination
is claimed compatible. Upgrade the trio deliberately and run the focused tests:

```powershell
$env:DATABASE_URL = 'sqlite://'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = '1'
python -B -m pytest -p pytest_asyncio.plugin -p no:cacheprovider -o asyncio_default_fixture_loop_scope=function backend/tests/test_article_extraction.py backend/tests/test_article_fetch_security.py backend/tests/api/test_articles.py -q
```

The transport replaces HTTPX's private `_pool` and subclasses httpcore's private
`_backends.anyio.AnyIOBackend`. HTTPX still maps httpcore errors and wraps response
streams. Inspect those interfaces on upgrades; the exact pins are intentional.

## Connection identity, DNS and pooling

Only the network backend substitutes an address: DNS is resolved once per new TCP
connection, every answer must be public, and AnyIO receives a numeric IP. Its
numeric-address branch does not resolve the hostname again. httpcore retains the
original scheme/host/port origin, sends that hostname to `start_tls`, and AnyIO
passes it to `TLSStream.wrap` for SNI and certificate verification. The SSL context
requires valid certificates and hostname checking. Caller SNI overrides are
discarded. Production clients disable environment proxies.

HTTP/1.1 pooling is explicitly enabled with HTTPX's usual limits (100 connections,
20 idle connections, five-second idle expiry); HTTP/2 is disabled. Reusing an open
same-origin socket cannot change its validated peer, so DNS is not rechecked for
that reuse. Reconnects resolve and validate again. Different origins cannot share
a connection even if DNS yields the same address. Redirects are checked at every
hop, with at most five redirects. Use `extract_articles` as the service entrypoint;
callers injecting a client into lower-level helpers must use `PublicHTTPTransport`.

## Timeouts and decompression

The connect timeout covers DNS and all numeric TCP attempts together. httpcore
still supplies connect timeouts to TLS, read timeouts to reads, write timeouts to
writes, and pool timeouts to queued requests, with HTTPX exception mapping intact.
The 20-second total download deadline additionally spans all redirects and reads;
it maps to the existing `timeout` status. It does not include HTML extraction.

Downloads and decompressed output are independently capped at 2 MiB. Raw streaming
avoids HTTPX's automatic unbounded decompression. Gzip and zlib-wrapped deflate are
decoded once with `max_length=limit+1`; no unbounded flush is called. Invalid
checksums, truncated streams, trailing bytes, concatenated gzip members, raw
deflate and unsupported encodings (including Brotli) fail closed. Clients request
identity encoding. Decompression input/output are bounded, but there is no hard
CPU deadline or process-level memory isolation for zlib or the HTML parser.

## Remaining boundaries

Address validation cannot detect infrastructure that routes a public IP to an
internal service, a public proxy that itself fetches internal resources, or
deployment-specific NAT64 prefixes outside the explicitly blocked ranges. Apply
network egress policy for those environments. Public HTTP/HTTPS services on
nonstandard ports are allowed. HTTPS-to-HTTP redirects remain allowed for article
compatibility. Concurrency is limited per batch, not globally across requests.
The tests use fake sockets/DNS and TLS boundary assertions; they do not establish
live Internet connectivity or perform a real peer certificate handshake.

## Extraction response provenance (Phase 7H.4B)

`POST /api/v1/articles/extract` retains its request, HTTP behavior and existing
`url`, `article_text`, `extraction_status`, and `extraction_error` property names.
There is no new response model. `fetch_provenance` is an additive **optional**
object: clients must accept its absence and must check `schema_version` before
interpreting it. Version 1 has these fields:

| Field | Meaning |
| --- | --- |
| `schema_version` | Integer `1` |
| `final_url` | Sanitized terminal URL or null; never inferred from the submitted URL or last attempted redirect |
| `redirect_chain` | Up to five accepted redirects, each with nullable sanitized `source_url`, nullable sanitized `target_url`, and integer `status_code` |
| `redirect_chain_complete` | Whether non-redirect terminal response headers were observed |
| `response_received_at` | UTC RFC3339 timestamp at terminal response headers, or null |
| `text_length_chars` | Actual returned text length after stripping and Core's existing cap; 0–20,000 |
| `text_length_utf8_bytes` | Actual returned text UTF-8 byte length; 0–80,000 |
| `text_truncated` | Whether Core's 20,000-character cap shortened extracted text |
| `response_host` | Terminal hostname or null; withheld when its URL is withheld |
| `publisher_canonical_url` | Always null; no canonical validation exists |
| `intended_article_resolved` | False for unresolved Google News wrappers; otherwise null |
| `source_identity_status` | `not_established`, `unverified`, or `google_news_wrapper_unresolved` |
| `urls_redacted` | Whether URL components were removed or URLs withheld, including the legacy `url` |

An accepted redirect means its Location was validated and selected for the next
request. It does **not** assert successful connection to that target, validated
publisher identity, or article resolution. Rejected Locations and the sixth
redirect are never appended. A subsequent DNS/destination security rejection
withholds all URL values, including previously accepted redirect entries.
No DNS answers, peer addresses, request/response headers, cookies, authorization
values or exception details enter this object.

HTTP errors can have terminal response provenance. A body-limit, read-timeout or
parser failure after terminal headers can also have it. Connection failures,
redirect rejection and redirect exhaustion before terminal headers have null
`final_url`, `response_host` and `response_received_at`, and a false
`redirect_chain_complete`. The timestamp describes response receipt at Core,
never publication time or completion of body retrieval.

Success means extractable text passed the existing minimum length. Neither
success nor `text_truncated=false` claims complete publisher article retrieval:
the publisher, response, parser or upstream service may already have omitted
material. `response_host` and redirected URLs never authenticate a publisher.
Google News wrapper URLs are withheld, not decoded. A terminal wrapper remains
unresolved even if its page produces extractable text. An ordinary redirect to
another host remains unverified, with resolution unknown.

Each disclosed URL is at most 2,048 UTF-8 bytes. Provenance is built from a fixed
field set and at most five entries; its default JSON representation, including
ASCII escapes, is checked against 32 KiB. An oversized object is omitted rather
than truncated into misleading evidence. Bounds do not change transport limits.

## URL disclosure and nullable legacy URLs

The legacy `url` now permits **JSON null**. All URL queries and fragments are
removed, regardless of parameter name. There is no query-secret blacklist.
Userinfo, invalid or syntactically unsafe destinations, control characters,
encoded/path punctuation outside `/A-Za-z0-9_.~-`, path segments over 128
characters, credential endpoint segments and Google News wrappers cause
withholding. URLs exceeding the disclosure byte cap are withheld, never cut.
DNS/destination security rejection also withholds the legacy URL.

Sanitized strings are absolute HTTP(S) display references. Removing a query can
change resource semantics. **Do not use a sanitized URL as a cache key, canonical
identity, deduplication identity, publisher assertion, or a guaranteed article
navigation target.** A null URL is unavailable, never an empty link or the text
`"None"`/`"null"`. Clients should hide/disable unavailable links and retain their
own submitted source identity separately when needed. Provenance cannot grant
article-backed evidence eligibility by itself. Generic, ordinary URL path and
hostname text remains untrusted source data; this policy is not a content or
publisher verifier.

## Sanitized diagnostics and logs

Extraction errors now use fixed messages. Generic network failures return
`Article request failed.` and unsafe destinations return
`Unsafe article destination.` Unsupported-content errors do not echo the remote
Content-Type. HTTP errors retain `HTTP <status>`. Existing statuses, minimum
article length, character cap and endpoint 200/401/403/422/500 behavior remain.
Internal logs retain only fixed categories: `http_status`, `timeout`,
`unsafe_destination`, `redirect_limit`, `unsupported_encoding`,
`invalid_content_length`, `download_limit`, `invalid_compression`, `download_rejected`, `request_error`,
`unexpected_error`, and endpoint `service_error`. No raw exception or traceback
is logged by extraction code.

HTTPX INFO logs contain raw URLs and httpcore DEBUG logs can contain headers and
exceptions. Context-local filters suppress those dependency logs during article
fetching, parsing and batch cleanup, including parser worker threads. Parser
dependency loggers present when extraction loads are filtered too. Unrelated
requests retain their normal logging, and cancellation restores logging context.
Recheck the dependency logger inventory when upgrading the pinned transport or
parser stack. Application ingress, reverse-proxy logging and arbitrary injected
client hooks are outside this extraction logger boundary and need their own
deployment policy.

Offline validation runner (cached Docker images required for `--services`):

```powershell
.venv\Scripts\python.exe -B backend/tests/phase_7h4b_runner.py backend/tests/test_article_extraction.py backend/tests/test_article_fetch_security.py backend/tests/test_article_provenance.py backend/tests/api/test_articles.py backend/tests/test_auth.py backend/tests/test_permissions.py backend/tests/test_core_import_boundary.py -q --tb=short
.venv\Scripts\python.exe -B backend/tests/phase_7h4b_runner.py --services -q --tb=short
```

The runner disables dotenv and bytecode/cache writes, refuses unexpected network
targets (including Windows asynchronous ConnectEx), and provisions only uniquely
named, labeled test containers on a dedicated network. PostgreSQL and Redis use
tmpfs storage and random loopback ports; Redis persistence is disabled and its
run ID verified. Existing migration tests run only in disposable databases;
no repository migration is created or changed. Task resources are removed even
on failure. This does not authorize live provider requests.
