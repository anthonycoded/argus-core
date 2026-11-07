# Core AI quotas and inference admission

`POST /api/v1/ai/generate` retains its request fields, success envelope,
`ai:generate` scope, model selection and provider-error mappings. Global Chess
relevance classification is unchanged in this phase and still uses its temporary
direct Ollama client. No endpoint, database schema, or migration is added.

## Operator configuration

The default AI allowance remains **10 requests per rolling 60 seconds**, checked
for both the authenticated application and API key. The general **120/minute**
application/key quota also applies. Other operation quotas are unchanged.

`AI_APPLICATION_QUOTA_OVERRIDES` is an operator-managed JSON object mapping
canonical lowercase application UUIDs to integer allowances from **1 through 60**.
Its default is `{}`; at most 100 overrides are accepted. For Global Chess, 60/minute
is an initial capacity-planning ceiling, **not guaranteed processing throughput**.

To configure it, use the already provisioned Global Chess `core_applications.id`
verified through trusted application administration. Set the object entry for that
exact ID to 60. No Global Chess identity is generated or inferred here. Application
names, slugs, incoming headers, and request-body fields cannot select an allowance.
Unknown IDs confer no access; authentication must first verify an active key and
its active application. Multiple keys still share the application's single bucket.
This allowance is shared with topic analysis, planning, evaluation and synthesis;
it does not reserve classification capacity.

Malformed JSON, duplicate IDs, noncanonical UUIDs, noninteger quotas, and out-of-range
configuration fail settings validation before Core starts. Validation hides input
values. Only operators who control the Core process configuration can set overrides;
there is no configuration API. Deploy consistent settings across all workers.
This change does not edit environment files or provision credentials.

Additional settings:

| Setting | Default | Accepted values |
| --- | --- | --- |
| `AI_MAX_CONCURRENT_INFERENCES` | 1 | Integer 1-8 |
| `AI_INFERENCE_LEASE_SECONDS` | 210 | Integer 200-300 |

All workers targeting one Ollama backend must use the same Redis logical database
and admission namespace. This implementation protects one shared inference backend;
separate Redis instances do not coordinate. Redis Cluster deployments must separately
validate the existing rate limiter's multi-key compatibility; this does not introduce
Cluster support. Admission's keys share one Redis hash tag.

## Admission and failures

An atomic Redis script uses Redis server time to expire owners and admit up to the
configured number of operations. The two fixed keys contain bounded owner tokens
and policy fields. A policy mismatch between workers fails closed while any owner
is active. Configure capacity consistently during rolling deployment; idle admission
can adopt a new policy. There is no pending inference queue, background retry, or
fallback provider. Request rate quotas are consumed before admission, including
when admission is subsequently busy.

Capacity exhaustion returns **503**, a fixed sanitized message, and integer
`Retry-After` between 1 and 5 seconds. This is retry guidance, not a reservation or
completion estimate. Redis/policy failures return **503 / Retry-After: 5**. Redis
commands have a two-second deadline. Existing quota exhaustion remains **429** with
the existing Redis-window guidance. General quota enforcement remains independent.

Only the request's cryptographically random token can release its slot. An expired
owner cannot delete a newer owner's slot. A lost acquire reply triggers one bounded,
token-checked cleanup attempt. Cleanup failures preserve the original API result,
log only a fixed message, and leave expiration to recover capacity. Redis must use
`noeviction` with adequate memory: eviction, flushing, failover data loss or a Redis
restart can erase admission state while provider work remains active. Stop/drain
inference before such maintenance. Cross-worker ownership assumes healthy shared
Redis; this is not a distributed consensus or exactly-once execution protocol.

## Cancellation, deadlines, and shutdown

Each admitted request owns one awaited asynchronous task for admission, the HTTP
operation and cleanup. A cancelled request never detaches it. Native asyncio and
AnyIO caller cancellation are joined until the owned work terminates; repeated
caller cancellation does not release capacity prematurely. Cancellation observed
during admission prevents model dispatch. Started work is allowed to finish, within
its original requested timeout, before cancellation propagates. Cancellation after
the provider result still joins token cleanup and does not return a success result.

The request's existing timeout field now also bounds the complete asynchronous
provider operation, including response parsing, rather than only HTTPX phase
inactivity. Deadline expiry remains **504**. It is cooperative: a blocked event loop
or an operation suppressing cancellation can exceed it. No parser thread or
fire-and-forget inference worker is introduced. Normal ASGI graceful shutdown must
allow active requests to drain; configure its grace period above the maximum
180-second model deadline plus bounded admission/cleanup and audit time.

Leases do not renew. Their 200-second minimum exceeds the 180-second maximum model
deadline plus ordinary Redis/cleanup overhead. A crashed/killed worker leaves its
slot until expiry. A suspended process, event-loop stall, forced shutdown or Redis
state loss can outlive/lose that protection. Core can establish that its HTTP
operation has terminated; it cannot prove remote Ollama GPU work stopped after a
network failure, timeout or worker death. Ollama exposes no completion receipt here.
Treat the lease duration as crash-recovery policy, not remote cancellation proof.

## Tuning and deployment

Keep concurrency at one initially. Measure model latency, memory and simultaneous
research workload before raising it; increasing requests/minute cannot create GPU
capacity. A 150-candidate job can exceed a minute even at a 60/minute AI allowance.
Discovery, pagination and other calls also consume the unchanged general quota.
Callers must surface incomplete work and validated retry guidance without blind
retries; relevance migration and its partial-result handling are Phase 6D.2.

Restart Core with validated operator configuration; no migration or automatic data
change is required. Preserve existing audit configuration. Successful requests,
authentication/permission failures, quota failures and admission failures continue
through the same request-audit middleware; prompts, keys and request bodies are
not added to audit records.

## Offline validation

`tests/test_ai_capacity.py` exercises operator validation, authenticated quota
selection, real Redis atomicity, stale ownership, cancellation and API/audit
contracts. Redis tests require `ARGUS_AI_TEST_REDIS_URL` and
`ARGUS_AI_TEST_REDIS_RUN_ID`, and refuse default Redis ports, non-loopback hosts,
incorrect run IDs or enabled persistence. Use only a verified disposable Redis.
Provider operations are mocked. Existing Core API and research contract tests inject
admission doubles and continue to run without live providers.
