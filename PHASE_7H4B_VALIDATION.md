# Phase 7H.4B validation

Date: 2026-11-11. Implementation is confined to Argus-Core. The operator supplied
the approved provenance schema in the implementation conversation after confirming
that the original audit had not been saved to disk. Workspace-write access was
available; the starting tracked worktree was clean at `e4f9cc9`.

Final focused validation: **183 passed in 29.85 seconds**. Final complete Core
backend suite: **849 passed in 82.08 seconds**. Both runs had zero failures,
errors or skips and one existing Starlette deprecation warning.

## 1. Exact file inventory

New files:

| File | Purpose |
| --- | --- |
| `backend/app/services/article_provenance.py` | Conservative URL disclosure, version-1 provenance, serialized size guard, context-local dependency-log filtering |
| `backend/tests/test_article_provenance.py` | Offline provenance, compatibility, redaction, nullability, diagnostics, logging and authorization regressions |
| `backend/tests/phase_7h4b_runner.py` | Offline validation runner; verified disposable service provisioning and cleanup |
| `PHASE_7H4B_VALIDATION.md` | This validation report |

Modified files:

| File | Purpose |
| --- | --- |
| `backend/app/services/article_extraction.py` | Observe accepted redirects and terminal headers; add optional provenance; sanitize legacy URL/errors/logging |
| `backend/app/api/v1/articles.py` | Replace exception traceback logging with a fixed endpoint diagnostic category |
| `backend/tests/test_article_extraction.py` | Assert the fixed unsupported-content message |
| `backend/tests/test_article_fetch_security.py` | Real transport/fake-wire DEBUG logging regression |
| `backend/tests/api/test_articles.py` | Assert sanitized null URL and optional provenance on SSRF rejection |
| `backend/ARTICLE_FETCH_SECURITY.md` | Public schema, compatibility change, disclosure rules, diagnostics and offline validation instructions |
| `pytest-core.ini` | Include the new provenance tests in the explicit Core profile |

No other repository files are part of this change. Transport, dependencies,
authentication, scope definitions, database schema and migration files are
unchanged. No Argus or Global Chess files were modified.

## 2. Response contract changes

`POST /api/v1/articles/extract` preserves its request and existing response
property names. `url` is now a sanitized absolute HTTP(S) display reference or
JSON null. All queries/fragments are removed; URLs that cannot satisfy the
disclosure rules are withheld. Nullability is an intentional approved legacy
contract change, including on successful text extraction.

`fetch_provenance` is additive and optional, with `schema_version=1`,
`final_url`, `redirect_chain`, `redirect_chain_complete`,
`response_received_at`, `text_length_chars`, `text_length_utf8_bytes`,
`text_truncated`, `response_host`, `publisher_canonical_url`,
`intended_article_resolved`, `source_identity_status`, and `urls_redacted`.
Redirect entries use `source_url`, `target_url`, and `status_code`. All disclosed
URL fields may be null. Oversized optional provenance is omitted without
changing legacy extraction results. The complete field semantics are documented
in `backend/ARTICLE_FETCH_SECURITY.md`.

No new public response model was introduced. The full OpenAPI document still
matches `backend/tests/fixtures/core_openapi.json` exactly, including the existing
untyped extraction response. That unchanged snapshot does not describe runtime
URL nullability or the additive object; the security documentation does.

`extraction_error` now contains fixed safe messages rather than exception text or
remote Content-Type values. HTTP status errors retain `HTTP <status>`.

## 3. Security improvements

Every URL query is discarded, including unknown parameter names, without relying
on a secret-parameter blacklist. Userinfo, syntactically unsafe destinations,
control characters, encoded/punctuated or long opaque paths, credential endpoint
segments and Google News wrapper URLs are withheld. Each disclosed URL is capped
at 2,048 UTF-8 bytes; URLs are never shortened into malformed strings. A transport
destination-security rejection clears legacy and provenance URL values. A
withheld final URL also withholds `response_host`.

No exception strings, tracebacks, headers, cookies, authorization values, DNS
answers or connection-peer observations are copied into public provenance.
Errors and extraction-owned logs use fixed messages and bounded internal
categories. Unknown download categories collapse to `download_rejected`.

HTTPX INFO and httpcore DEBUG can otherwise reveal raw URLs, headers or exception
details. Context-local filters suppress those dependency details during fetching,
parsing and batch cleanup; parser worker threads inherit the context. Tests cover
real transport code with mocked DNS/wire data, successful and failed requests,
parser failure, concurrent unrelated logging, and cancellation. Unrelated
requests retain normal logging.

All existing transport boundaries remain: five redirects, validation at each
hop, public-only DNS answers, DNS pinning, numeric connection attempts, original
Host/TLS identity, verified TLS certificates, ignored caller SNI overrides,
environment-proxy isolation, pool/origin boundaries, 2 MiB compressed and decoded
body limits, existing encoding/integrity checks, the 20-second total download
deadline and existing phase timeouts. `article_fetch.py` is unchanged.

## 4. Backward compatibility analysis

All existing extraction statuses remain: `failed`, `success`, `http_error`,
`timeout`, `request_error`, `unsupported_content`, `no_content`, and
`insufficient_content`. The endpoint still returns extraction outcomes under
HTTP 200, malformed request URLs under 422, missing authentication under 401,
missing scope under 403, and its existing no-result/service errors under 500.
Authentication, quotas, scope checks, parser options, minimum text length,
character cap and source metadata preservation are retained.

Compatibility exceptions approved for this phase are nullable/sanitized legacy
URLs and fixed error messages. Consumers must not compare raw diagnostic strings,
assume every result URL is a string, or interpret sanitized display references as
equivalent resource identities. Removing a query can change navigation semantics.
Unavailable links must be hidden/disabled rather than rendered as `null`, `None`,
an empty URL, or a concatenated relative link.

Endpoint tests exercise both a successful extraction with a withheld URL and
security rejection with a withheld URL, JSON null serialization, safe absolute
string serialization, legacy property preservation, missing optional provenance,
and existing authorization/error HTTP behavior. Read-only inspection found no
current extraction client or extraction-result URL rendering in Argus; this phase
therefore cannot claim end-to-end compatibility for a future Argus client or
uninspected external consumers. No consumer code was changed.

## 5. Provenance correctness

Redirect entries record observed redirect statuses whose Locations were validated
and accepted for a follow-up attempt. They do not claim a successful connection
to the target. Rejected Locations and the sixth redirect are not appended.
`redirect_chain_complete` becomes true only at a non-redirect terminal response.
HTTP errors can therefore have terminal provenance; a missing-Location redirect
cannot. The URL, hostname and UTC RFC3339 timestamp come from that observed
terminal response boundary, never from a guessed last target.

Connection failures, pre-terminal timeouts, rejected redirects and redirect-limit
failures have no invented terminal URL or timestamp. Failures after terminal
headers retain those observations without claiming completed body retrieval.
Returned-text counts are computed after stripping and the existing 20,000-character
cap; four-byte Unicode tests reach the 80,000-byte maximum. `text_truncated` is true
only when Core's character cap shortened the extracted text.

The fixed field set, five-entry cap, individual URL caps and final serialized
32 KiB check bound provenance. Tests exercise URL boundary sizes, maximum-size
redirect chains, multibyte text and omission of oversized optional provenance.

`publisher_canonical_url` is always null. `intended_article_resolved` is false for
an unresolved Google News wrapper and otherwise null. Wrapper content is never
decoded; extractable wrapper-page text does not establish the intended article.
A redirect away from a wrapper still yields only `unverified` identity, not
publisher verification. Success and `text_truncated=false` never assert complete
publisher article retrieval.

## 6. Test counts, runtimes and warnings

Final implementation checks:

| Check | Result |
| --- | --- |
| Focused extraction/provenance/endpoint/transport/auth/scope/OpenAPI/import packet | 183 passed, no failures/errors/skips, 1 warning; 29.85 s |
| Complete Core backend suite | 849 passed, no failures/errors/skips, 1 warning; 82.08 s |
| Exact full OpenAPI snapshot and Core-only import boundary | Passed in focused and complete suite |
| `pip check` | No broken requirements |
| Pinned transport dependency checks | HTTPX 0.28.1, httpcore 1.0.9, AnyIO 4.15.1; passed |
| Python AST and runtime import scan | Passed for changed/new Python files; no new consumer imports |
| Secret-pattern scan | No unexplained matches; five exact synthetic credential-URL fixtures accounted for |
| Whitespace/final newlines and `git diff --check` | Passed, including new untracked files |

Environment: existing Core virtual environment, Python 3.12.6. Each successful
pytest run emits the existing `StarletteDeprecationWarning` concerning HTTPX with
Starlette TestClient. No dependency upgrade was made. Git also reports ordinary
LF-to-CRLF normalization notices and access warnings for preexisting pytest cache
directories; cache writes are disabled and those directories were left alone.

Earlier validation attempts, superseded by the final runs:

- Initial guard prototype blocked Windows asyncio socketpair setup: 14 passed,
  98 setup errors, 50 warnings in 14.18 s. The guard was corrected to allow only
  local socketpair construction; no live network was allowed.
- Corrected pre-change offline baseline: 112 passed, 1 warning in 22.64 s.
- Interim implementation focused run: 175 passed, 1 warning in 28.71 s.
- Docker internal-network preflight did not publish required host ports; it
  stopped before pytest and removed its resources. A dedicated bridge plus the
  connection guard was used instead.
- Initial complete run: 835 passed, six source-list failures and three migration
  setup errors, 1 warning in 81.17 s. The harness had not initialized the legacy
  source-list tests' global disposable database and had used an inaccessible
  shared pytest temp directory. Neither issue was an extraction regression.
- Corrected full run before the last five regressions: 844 passed, 1 warning in
  79.46 s. Final focused/full checks include those last regressions and the
  bounded fallback diagnostic category.

Reproduction commands, from the Core repository root:

```powershell
.venv\Scripts\python.exe -B backend/tests/phase_7h4b_runner.py backend/tests/test_article_extraction.py backend/tests/test_article_fetch_security.py backend/tests/test_article_provenance.py backend/tests/api/test_articles.py backend/tests/test_auth.py backend/tests/test_permissions.py backend/tests/test_core_import_boundary.py -q --tb=short --durations=5
.venv\Scripts\python.exe -B backend/tests/phase_7h4b_runner.py --services -q --tb=short --durations=10
.venv\Scripts\python.exe -B -m pip check
git -c safe.directory=C:/Users/barbe/Desktop/projects/argus-core diff --check
```

The explicit Core pytest profile includes the new module. No global Git settings
were changed; `safe.directory` was supplied only per command for this sandbox's
different Windows account ownership.

## 7. Remaining limitations

Provenance describes Core observations, not article authenticity, factual
verification, publisher identity or completeness. The wall-clock UTC receipt
timestamp is not publication time. Sanitized URLs can lose resource semantics;
ordinary path/hostname text remains untrusted source input. Conservative
withholding can reduce available links. No canonical validation, browser,
Google News decoder, publisher-verification system or evidence store was added.

Extraction of HTML itself still has the previously documented parser CPU/memory
limitations. Network egress/NAT/public-proxy limitations in the existing transport
documentation remain. Dependency logger inventories must be rechecked on upgrades;
reverse proxies, application ingress and arbitrary caller-supplied logging hooks
need their own policy. Tests establish fake-network transport boundaries, not
live Internet behavior or a real certificate handshake.

## 8. Argus integration implications

Argus was inspected read-only and remains unmodified by this phase. It must accept
missing/unknown-version provenance and nullable URLs before integrating this
endpoint. Original source identity should be retained separately from sanitized
display references; response URLs must not replace stable source/cache identity.
Successful text with an unavailable URL is a valid outcome, and link rendering
must treat null as unavailable.

Provenance alone cannot authorize article-backed evidence admission or automated
delivery. Unresolved wrappers, unverified hosts and incomplete/failed extraction
remain unsuitable for claims of verified publisher evidence. No API-key scope,
extraction activation, evidence-policy or automatic Telegram gate was changed.

## 9. Confirmation of no live operations

No live discovery, extraction, inference, provider, browser or Telegram request
was made. Dotenv was disabled before application imports; application credentials and
API-key scopes were not loaded or changed. Tests use mocked providers and fake
DNS/wire data. Connections are restricted to task-owned disposable loopback ports
(including Windows asynchronous ConnectEx), apart from local asyncio socketpair
construction. Existing migration tests operate only on disposable databases;
no migration file was created or changed.

Service tests use cached images with `--pull=never`, fresh uniquely named/labeled
PostgreSQL/Redis containers on a dedicated network, random loopback ports and
tmpfs storage. Redis persistence is disabled and run IDs checked. Development
containers/databases were not used. Every task container/network was removed;
fresh task-owned pytest directories were removed after verifying their resolved
paths stay under Core and are not reparse points. No persistent database was
modified. No commit, push, merge or deployment occurred.
