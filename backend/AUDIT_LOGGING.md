# Request audit logging

Argus-Core (`app.core_main`) enables request auditing through the shared factory's
optional `audit_session_factory`. The legacy Global Chess entrypoint is unchanged.
HTTP requests under `/api/v1` generate one `core_audit_logs` row, except successful
`/api/v1/health` requests. Documentation endpoints and non-HTTP scopes are excluded.

Records contain a UTC request-start timestamp, method, route template, response
status, duration in milliseconds, and application/key IDs supplied by successful
API-key authentication. Authentication failures (401) have null IDs; permission
failures (403) and rate limits (429) retain the authenticated IDs. Missing routes
use `/<unmatched>`; path-parameter values and query strings are never stored.
Unhandled errors before response headers are recorded as 500 and re-raised so
FastAPI retains its normal error behavior. Errors after response headers retain
the status already sent to the client. Duration excludes the audit write but
includes response streaming and application background tasks.

No API keys, authorization headers, other headers, request/response bodies, query
parameters, raw URLs, exception text, or untrusted unmatched paths are persisted.
The existing model and migration have **no client IP or user agent columns**;
these fields are deliberately omitted rather than stored in another field. Adding
them requires a separately reviewed schema migration and privacy/retention policy.

Persistence runs off the event loop after endpoint cleanup in a separate session
using a dedicated audit pool: one insert/commit, no identity lookups and no
new engine per request. Failure rolls back/closes that session and emits a fixed,
sanitized error message; it never replaces an API response or application error.
The middleware does not change existing last-used API-key writes. Argus-Core
installs this middleware exactly once; the audit pool is disposed at shutdown.

## Timeout and capacity policy

The production PostgreSQL audit pool has two connections, no overflow, and a
one-second checkout timeout. New connections have a two-second connect timeout;
session startup options set statement timeout to one second, lock timeout to
500 milliseconds, and idle-in-transaction timeout to two seconds. These settings
affect audit connections only, not endpoint sessions. No per-write SET queries
are needed. An additional three-second deadline bounds how long request handling
waits for audit work, including thread scheduling. Injected test session factories
retain their own database configuration; they still get the middleware deadline.

Python cannot forcibly stop a synchronous database driver thread. On deadline the
wait is abandoned, and a write may complete later. A nonblocking semaphore bounds
actual writers to two per middleware instance, including abandoned workers; excess
records are dropped with the same sanitized failure log. Network failures can
outlast database-side timeouts. No retries occur. These are best-effort records,
not an exactly-once or lossless delivery guarantee.

## Operational limits

This is best-effort logging, not a durable queue or compliance ledger. Process
termination, request-task cancellation, database outages, or concurrent deletion of
an application/key can lose records. Audit writes have no retry to avoid amplifying
outages. They occupy a bounded thread/pool connection and can delay request-task
completion up to the middleware deadline. Under
sustained load, consider a bounded queue and batch writer with explicit loss
metrics. Successful health probes are excluded, but unauthenticated attack traffic
can still cause substantial write volume. Configure ingress limits, retention,
archival, monitoring and restricted database access separately. This implementation
does not create schemas or run migrations; the existing audit migration must have
been applied before deployment.
