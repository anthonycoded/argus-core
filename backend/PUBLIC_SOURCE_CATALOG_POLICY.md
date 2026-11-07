# Phase 5D.2A: offline public-source catalog policy

`app.core.source_catalog` is a pure policy/decision library. Phase 5D.2B.1's internal
admission infrastructure reuses its strict facts contracts; no live route, client,
or startup path applies its admission, projection or identity decisions yet.
It does not connect, resolve DNS, fetch, register, merge, update or publish records.
Global Chess automatic registration remains disabled.

Durable storage mechanics and their remaining authority boundary are documented
in [SOURCE_ADMISSION_INFRASTRUCTURE.md](SOURCE_ADMISSION_INFRASTRUCTURE.md).

## Trust and public facts

Public means shareable among authorized applications, not anonymously readable.
`CatalogContext` must come from future trusted authentication/policy code. It
requires an explicit nonzero registry UUID, application ID and strict boolean read
permission, defaulting to denied. The application ID never enters public output.

`TrustedAdmission` is a server-only decision bound to a registry UUID and exact
source ID. It defaults to unreviewed. Its states are public, unreviewed and private;
the latter two are equally invisible. No constructor infers admission from stored
or caller metadata. Constructing these Python objects is a trust-boundary operation,
not proof of authorization: future API adapters must not deserialize them from a
request body or allow a client to select the permission or admission state.

`CatalogEntry` takes a detached mapping, not an ORM object. The caller must obtain
consistent lookup snapshots without lazy database attributes. Admission is tested
before reading or validating record fields. A denied application, wrong registry,
unreviewed/private admission or grant for another source yields no public record.

The projector selects only source ID, canonical URL, domain, title, publisher,
publication date, source type and stored fingerprint, with explicit registry and
algorithm context. It never reads `metadata`, `metadata_json`, original stored URL,
creation time or arbitrary extra columns. The public DTO forbids extra fields.

Title, publisher, date and source type remain **registered observations**, even
after public admission. Output always says `claims_verification:
"not_independently_verified"`. Admission attests permission to expose the selected
values; it does not verify the article or publisher. IDs/keys are technical catalog
facts, not proof of editorial truth. Code cannot recognize every private string in
an otherwise allowed title/publisher field. Review must cover the projected values,
not merely whether a URL looks public. Editorial notes, relevance, snippets, topic
relationships, research queries, credentials and arbitrary provenance dictionaries
have no fields in these contracts.

Facts have character/type limits aligned with existing source columns: ID up to
36 path-safe ASCII characters, title 1000, publisher 500, source type 100, domain
255, URLs 2048 characters/8192 UTF-8 bytes, lowercase 64-hex fingerprints. Dates must
be aware datetimes; unknown dates are null. No legacy timezone is invented. URL
syntax rejects credentials, obvious internal names/private literal addresses,
transition addresses and common credential-query names. It preserves valid input
strings; it neither rewrites nor dereferences URLs. These checks cannot establish
DNS destination safety or classify every sensitive query. Future fetches still
require the DNS-pinned transport and an approved public-source/query policy.

## Explicit keys and versioned fingerprints

`IdentityQuery.canonical_url` is an explicit lookup key. `core-url-normalizer-v1`
names the current pure `canonical_source_url` behavior, not an extra HTTP/Pydantic
URL rewrite. The query and admitted stored keys must already satisfy that function;
otherwise they fail validation/review. No legacy IDs, URLs, domains or hashes are
replaced or repaired. A compatible stored key is emitted byte-for-byte. New HTTP
wire adapters must explicitly reconcile their URL serialization step with this
version; silent parser/normalizer upgrades are unsafe.

`observed_url` and `observed_publisher` remain separate caller observations. The
engine does not derive an alias/redirect relationship between an observation and
the explicit canonical key. A future adapter owns that provenance assertion.
Public output can echo query values already known to the caller; hidden record
values never supplement them.

`fingerprint_full_content` requires explicit nonblank full-content input, capped
at 250,000 characters/1 MiB UTF-8. It rejects whitespace-only text before calling
the unchanged hash helper. `core-content-sha256-v1` is the existing lowercase,
collapsed-whitespace SHA-256 algorithm. The digest of empty normalized content is
not a valid new full-content claim. Existing stored hashes are never rewritten.
Neither a digest nor the `full_content_claim` label proves full-article provenance,
public permission, publication identity or independent corroboration.

Historical source rows have no stored hash version. `TrustedAdmission` therefore
defaults `fingerprint_version` to null; the projector does not invent one. Only a
trusted adoption process can attest a known version. Unversioned URL records can
still match by URL, with explicit `version_unknown` and review required when a
fingerprint was supplied. An unversioned record is not accepted in a versioned
content-match lookup slot.

## Decision semantics

`decide_identity` takes at most one URL lookup and one versioned content lookup,
reflecting the current unique indexes. It projects visibility first. Slot inputs
must match the query; conflicting snapshots of the same ID are rejected with a
constant internal diagnostic. It performs no queries, reservations or mutation.

| Visible eligible lookup state | Outcome | Content verification | Review |
| --- | --- | --- | --- |
| No matches, no fingerprint | not_found | not_supplied | false |
| No matches, fingerprint supplied | not_found | not_found | false |
| URL match, no fingerprint | url_matched | not_supplied | false |
| URL and versioned fingerprint agree on one ID | url_matched | matched | false |
| URL match has no stored hash | url_matched | missing | true |
| URL match has unknown hash version | url_matched | version_unknown | true |
| URL record has a different known-version hash | conflict | disagrees | true |
| URL A and versioned hash B identify different records | conflict | disagrees | true |
| No URL match; same versioned content at another URL | related_content | matched | true |

Only `url_matched` contains an authoritative `source`. Related-content decisions
contain one public candidate and conflicts at most two; neither authorizes binding,
aliasing or merging. Conflict codes are `url_content_disagreement` and
`url_hash_identity_disagreement`. The first can represent an updated article, not
necessarily abuse. Missing/unknown fingerprints request review of content evidence
without denying that the exact URL key matches.

Hidden inputs are equivalent to absent inputs for the same query and public
snapshot: serialized outcome, flags, verification, candidates and conflicts are
identical. Hidden IDs, URLs, stored hashes, counts and conflict existence are never
reported. Malformed hidden mappings are not touched. A visible URL's own mismatch
can still produce a conflict, identically whether a hidden hash match exists or
not. The library makes no constant-time or database access-pattern guarantee.

`not_found` does not grant registration/publication permission. A read-only preflight
is not a reservation. Future registration must rerun this matrix transactionally,
including after uniqueness races, rather than choosing one constraint's winner.

## Admission storage and legacy adoption: unresolved deployment decisions

There is currently no provisioned admission authority. Phase 5D.2B.1 implements
separate Core-owned catalog identity and append-only admission-event tables, with
internal transactional mechanics. These replace the earlier marker-versus-review-
table design options without adding columns to existing source rows. Their FKs
stay within Core. The policy library itself still performs no persistence.

Who may admit/revoke records and how an operator proves authority remain unresolved.
No ordinary request can construct a trusted execution context through an existing
API. Storage does not enforce live source visibility, and no legacy record is
assumed public. See the infrastructure document for migration, role separation,
canonical snapshots, reconciliation and downgrade restrictions.

Adoption must inventory data under separate authorization, preserve original rows
and IDs, classify projected fields and sensitive URLs, default to unreviewed, and
produce a reviewable report. Do not automatically delete duplicates, repair URLs,
fill hashes, infer versions or publish old arbitrary metadata. Existing public
evidence references must remain attributable to their observations.

The existing unique non-null content-hash index remains in place. It prevents
inserting a second publication with the same stored hash. Return review/conflict
outcomes rather than silently clearing a hash or binding the first publication.
Independent publication/content relationships require a later approved model/index
design. Historical migration identifiers and dependencies remain unchanged.

Use one explicitly provisioned stable registry UUID per logical catalog; preserve
it across restoration/failover and distinguish independent forks. Never generate
it per process or infer it from a URL/database name. Unit fixture UUIDs do not
configure the application. Namespace configuration/discovery is a later change.

## API rollout and live limitations

No live API path imports this module. Current source registration, duplicate
precedence, permissive metadata storage/disclosure, scopes, quotas, audit logging,
normalization, database constraints and Global Chess client contracts are unchanged.
The offline properties above are not yet enforced by those endpoints.

After admission authority is settled, add a separately typed resolution API and
strict public-registration path. Apply one projector/visibility policy to legacy
GET/list/count, batch/duplicate/conflict paths as well, so old endpoints cannot
bypass it. Unknown and hidden records must not be distinguishable. Hidden uniqueness
collisions can leak existence through writes: restrict writes to trusted operators
during adoption or explicitly design a uniform pending-admission workflow first.

Preserve existing successful response envelopes for compatible cases. The current
Global Chess source DTOs forbid unexpected fields, so do not append new namespace/
decision fields to them silently. Implement a separate new wire contract/client
method before rollout. If using a new API prefix, deliberately extend audit coverage
(currently `/api/v1`). Keep authentication/quotas and explicit write uncertainty.
Do not enable automatic research registration as part of that rollout by default.

Validation consists of pure decision/projection tests plus focused existing
normalization, registry and Global Chess client compatibility tests using only
mocked HTTP and in-memory SQLite. No live provider or persistent services are needed.
