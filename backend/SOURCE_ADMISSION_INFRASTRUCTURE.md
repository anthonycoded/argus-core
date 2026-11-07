# Durable source admission infrastructure — Phase 5D.2B.1

**Admission storage exists, but public-catalog enforcement is not yet enabled.**

Existing source APIs, metadata disclosure, URL-first/content-hash deduplication,
response envelopes, scopes and quotas remain unchanged. Global Chess registration
is not enabled. The internal repository has no route, startup caller, authority
issuer, identity provisioner, or administrative UI. Do not present this milestone
as repairing existing live source visibility.

## Ownership and schema

`core_source_catalogs` and `core_source_admission_events` belong exclusively to
CoreBase. Existing Core sources/indexes and Global Chess models/data are untouched.
No source gains admission merely by existing, having a public-looking URL, carrying
metadata, or being registered by an application with `sources:write`.

The catalog table has a SMALLINT primary key constrained to 1, a unique nonzero
UUID, and a database timestamp. Both new tables start empty. No UUID default,
migration insert, startup creation or provisioning method is supplied. Explicit
provisioning is a separately authorized operation, not part of application startup.
Repository readers require a caller-pinned expected UUID and fail closed if absent
or different. Registry-read failure does not disable legacy APIs in this milestone.

The event ledger has UUID event/operation identifiers, registry/source foreign keys
with RESTRICT deletion, a positive sequential decision version and expected prior
version, action, reason, version identifiers, reviewed facts/digest, trusted actor,
optional opaque review reference and a database-generated timestamp. Sources retain
their original IDs, URLs, hashes and metadata. The unique registry/source/version
index supports latest-event and descending bounded-history reads; the operation-ID
unique index supports reconciliation. No additional unrelated indexes are needed.

PostgreSQL enforces keys, foreign keys, positive version arithmetic, supported
policy/action/reason and approval-version combinations, snapshot presence/absence,
digest format, and a closed top-level JSON object with a coarse 32 KiB JSONB-text
bound. JSONB rendering adds spaces, so this guard is deliberately distinct from
the exact 16 KiB canonical-byte limit. Repository validation enforces the complete
strict public-facts shape/types/field limits, exact canonical size, digest agreement,
source/registry binding, matching current facts, actor/reference syntax, state
transitions and stream continuity. Direct SQL by a privileged writer can bypass
repository-only invariants; it must never be exposed to ordinary application code.

## Decisions and trusted execution

No events means unreviewed. `approve` is a public-admission decision;
`revoke` withdraws an existing latest approval; `reject` records non-public review
without pretending to revoke an approval. Rejecting an active approval is prohibited:
use explicit revocation. A fresh approval after rejection/revocation is a new event.
Reapproval never rewrites history. Reasons are a closed, action-bound vocabulary:
`public_facts_reviewed`, `public_disclosure_rejected`, `public_access_revoked`.

`TrustedAdmissionExecution` is a typed assertion from a trusted in-process caller,
**not authentication or a minted administrator capability**. Its constructor is not
a security authority. A future approved operator adapter must authenticate and
authorize the reviewer, derive stable actor issuer/subject, validate the review
artifact, and use restricted writer credentials. Existing API keys, application
slugs, request bodies and metadata must not construct it. No real reviewer, role,
key, credential or authority policy is provisioned here. Tests use synthetic actors
and source records exclusively in disposable databases.

The actor fields intentionally have no cascading FK to application/key rows;
deleting credentials must not erase the recorded decision identity. Opaque review
references cannot be URLs or free-form notes. Admission attests permission to expose
reviewed observations, not independent verification of news/publisher claims.

## Canonical facts and digest

`approved-facts-json-v1` is frozen as follows:

1. Validate the complete Phase 5D.2A `PublicSourceFacts` contract, rejecting unknown
   fields and malformed values; require a nonzero registry UUID.
2. Include every field, including defaults and explicit nulls. Sort JSON object
   keys lexicographically; use compact comma/colon separators and no whitespace.
3. Render UUIDs as lowercase hyphenated strings. Render aware publication dates
   in UTC with exactly six fractional digits and a trailing `Z`. Equivalent timezone
   offsets therefore hash identically. Unknown dates remain null.
4. Preserve all other strings byte-for-byte after UTF-8 encoding. Perform no NFC,
   URL rewriting, trimming, publisher rewriting, or legacy recanonicalization.
   JSON uses `ensure_ascii=False`; JSON-required escaping is retained. No floats,
   NaN or non-JSON values are accepted by the facts schema.
5. Reject canonical UTF-8 payloads exceeding 16,384 bytes. Compute lowercase
   SHA-256 of ASCII `approved-facts-json-v1` plus newline plus those canonical bytes.

JSONB key order is never hashed directly. Readback reconstructs strict facts and
checks the digest. Returned commands/events and nested facts are frozen DTOs, not
mutable ORM rows. Future canonicalization changes require a new explicit version
and reader/writer compatibility design; never reinterpret old snapshots silently.
Unknown historical fingerprint provenance remains null. Supplying a known version
is an explicit trusted attestation, not a conclusion derived from hash syntax.

## Repository and concurrency

`AdmissionRepository` requires PostgreSQL and a pinned registry UUID. It supports
identity verification, latest event, descending keyset history (1–100 events for
one explicit source), operation-ID reconciliation, and transactional append.
It creates no engine, namespace, source, or automatic decision itself.

Append revalidates the command/context, checks the namespace, obtains nonblocking
transaction advisory locks (operation first, source stream second), reconciles an
existing operation, checks expected latest version/state, and compares reviewed
public fields with the current source before insertion. It reads no source metadata.
One transaction inserts and commits one decision. Reads/approval never mutate
`core_sources`. Lock contention/stale versions return explicit conflicts without
queues or retries. Concurrent source-fact edits remain possible: a later visibility
adapter must compare current facts with the approved snapshot and fail closed on
drift. This milestone does not implement that adapter.

Lock keys use the first signed 64 bits of SHA-256 over a fixed versioned prefix,
lock kind, a null separator, fixed-width registry UUID bytes and the bounded source
ID. Operation locks deliberately use a zero UUID namespace so operation IDs are
global within the database. No Python `hash()` is used. A theoretical 64-bit
collision only causes conservative contention; full relational keys and operation
comparison still prevent identity mixing. Independent registries/sources have
distinct lock inputs. Lock release follows transaction completion/rollback.

Every command carries an operation UUID and expected prior version. Repeating the
same canonical command and trusted actor returns the original event; changing any
parameter/actor rejects reuse. Busy concurrent calls may reconcile after completion.
SQL failure before commit rolls back. A commit exception is explicitly **unknown**;
the repository never blindly retries or claims rollback. Call `reconcile` with the
same operation ID to inspect a committed result. A missing result does not itself
prove that a concurrent command cannot still finish; retain external uncertainty
and follow an approved operator reconciliation procedure.

Request audit logging remains best effort and separate from endpoint transactions.
It is not the durable decision ledger. No event mutation/deletion API is provided.

## Database privileges — reviewed deployment template, not executed

Use separately provisioned, non-owner, non-superuser, NOINHERIT roles with no
membership allowing escalation to the table owner. Replace the role names only
after reviewing deployment membership and default grants. This SQL is documentation;
the migration does not execute grants, create roles or touch Global Chess objects.

```sql
REVOKE ALL ON core_source_catalogs, core_source_admission_events FROM PUBLIC;
REVOKE ALL ON core_source_catalogs, core_source_admission_events FROM argus_runtime;
GRANT SELECT ON core_source_catalogs, core_source_admission_events TO argus_runtime;

REVOKE ALL ON core_source_catalogs, core_source_admission_events FROM argus_admission_writer;
GRANT SELECT ON core_source_catalogs, core_source_admission_events TO argus_admission_writer;
GRANT INSERT ON core_source_admission_events TO argus_admission_writer;
GRANT SELECT (id, canonical_url, domain, title, publisher, published_at,
              source_type, content_hash) ON core_sources TO argus_admission_writer;
```

Both roles also need reviewed schema USAGE/connect access. Neither gets UPDATE,
DELETE or TRUNCATE on these tables. The writer cannot provision/replace a namespace.
Any namespace provisioner is a separate, explicitly authorized role/process.
Audit all inherited roles and existing PUBLIC/schema/default grants: the template
cannot neutralize inherited privileges, ownership or an already overprivileged user.
The migration's owner/superuser can modify or destroy history; no trigger pretends
otherwise. Restricted-role tests demonstrate denials in disposable PostgreSQL,
not the privileges of any actual deployment. Full immutable/WORM audit guarantees
remain an operational policy decision, supported by protected backups/archival.

## Lifecycle, deployment and rollback

Keep the same UUID through ordinary backup, recovery, failover and credential
rotation. Independent forks need a newly provisioned UUID and a separately reviewed
history/admission adoption process; do not relabel old events. Fence divergent
writable failover instances. Older backups may lose revocations: reconcile recovery
to an approved ledger/WAL recovery point before future public-catalog reopening.
Revocation cannot retract facts previously downloaded by applications. Future
read-side enforcement must define in-flight transaction and cache semantics.

Revision `61c2a9e47b30` follows `dea3e0498bf6`, leaving every historical revision
unchanged. Deploy: provision infrastructure -> apply migration using the approved
migration role -> start the matching application version. Existing readiness still
performs only read-only schema/head checks; it now expects the two new Core tables
through CoreBase metadata. It does not provision/check namespace rows or run commands.
Exact-head readiness means an older binary cannot simply restart after this upgrade.
Coordinate deployment and rollback rather than stamping around revision checks.

Prefer forward repair. Downgrade takes exclusive locks and refuses removal if
either table contains any row, including a UUID without events. No automatic
archival bypass exists. A populated downgrade requires a separately approved
archive/recovery plan; implement no destructive override in this milestone.
Only empty-storage downgrade is supported and tested in disposable databases.
Legacy adoption, classification, automatic registration, public projections and
visibility enforcement remain disabled and require subsequent reviewed milestones.
