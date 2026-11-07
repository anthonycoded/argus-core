"""Offline public-catalog contracts and decisions. No ORM, I/O or route integration."""

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Annotated, Literal
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, TypeAdapter, field_validator, model_validator

from app.utils.deduplication import canonical_source_url, content_hash

POLICY_VERSION = "public-catalog-v1"
URL_VERSION = "core-url-normalizer-v1"
HASH_VERSION = "core-content-sha256-v1"
HashVersion = Literal["core-content-sha256-v1"]
_http_url = TypeAdapter(HttpUrl)


class CatalogInputError(ValueError):
    """Constant diagnostics only; never attach raw records or private values."""


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,35}", value):
        raise ValueError("Invalid source identifier")
    return value


def _text(value: str | None) -> str | None:
    if value is not None and (not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError("Invalid public text")
    return value


def _public_url(value: str) -> str:
    # Syntax checks only: no DNS, fetching, redirect resolution or URL rewriting.
    if ("\\" in value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
            or re.search(r"%(?![0-9a-fA-F]{2})", value)):
        raise ValueError("Invalid public URL")
    parsed = _http_url.validate_python(value)
    if parsed.username or parsed.password:
        raise ValueError("Credential-bearing URL")
    host = (parsed.host or "").strip("[]").rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Nonpublic URL")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in host.split(".")):
            raise ValueError("Invalid public hostname") from None
    else:
        if (not address.is_global or address.is_multicast or address.is_reserved
                or host == "168.63.129.16"
                or (address.version == 6 and (address.ipv4_mapped is not None or address.sixtofour is not None
                    or address.teredo is not None or address.is_site_local
                    or address in ipaddress.ip_network("64:ff9b::/96")
                    or address in ipaddress.ip_network("64:ff9b:1::/48")))):
            raise ValueError("Nonpublic URL")
    secret_keys = {"key", "api_key", "apikey", "token", "access_token", "password", "secret",
                   "authorization", "signature", "x-amz-signature"}
    if any(k.casefold() in secret_keys for k, _ in parse_qsl(urlsplit(value).query, keep_blank_values=True)):
        raise ValueError("Credential-bearing URL")
    if len(value.encode("utf-8")) > 8192:
        raise ValueError("Public URL exceeds byte limit")
    return value


class CatalogContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)


class ContentFingerprint(CatalogContract):
    version: HashVersion
    value: str = Field(pattern=r"^[0-9a-f]{64}$")
    basis: Literal["full_content_claim"] = "full_content_claim"

    @field_validator("value")
    @classmethod
    def nonempty_content(cls, value):
        if value == content_hash(""):
            raise ValueError("Empty normalized content is not a full-content fingerprint")
        return value


def fingerprint_full_content(content: str) -> ContentFingerprint:
    """Explicit full-content claim, not proof of publication or public permission."""
    if (type(content) is not str or not content.strip() or len(content) > 250000):
        raise CatalogInputError("Invalid full-content input")
    try:
        if len(content.encode("utf-8")) > 1024 * 1024:
            raise CatalogInputError("Invalid full-content input")
    except UnicodeError:
        raise CatalogInputError("Invalid full-content input") from None
    return ContentFingerprint(version=HASH_VERSION, value=content_hash(content))


class IdentityQuery(CatalogContract):
    canonical_url: str = Field(min_length=1, max_length=2048)
    normalization_version: Literal["core-url-normalizer-v1"] = URL_VERSION
    observed_url: str = Field(min_length=1, max_length=2048)
    observed_publisher: str | None = Field(default=None, max_length=500)
    fingerprint: ContentFingerprint | None = None

    _urls = field_validator("canonical_url", "observed_url")(_public_url)
    _publisher = field_validator("observed_publisher")(_text)

    @field_validator("canonical_url")
    @classmethod
    def unchanged_key(cls, value):
        if canonical_source_url(value) != value:
            raise ValueError("Supply an explicit canonical key; no automatic normalization")
        return value


class PublicSourceFacts(CatalogContract):
    registry_id: UUID
    source_id: str
    canonical_url: str = Field(min_length=1, max_length=2048)
    normalization_version: Literal["core-url-normalizer-v1"] = URL_VERSION
    domain: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=1000)
    publisher: str | None = Field(max_length=500)
    published_at: datetime | None
    source_type: str = Field(min_length=1, max_length=100)
    content_hash: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    content_hash_version: HashVersion | None
    claims_verification: Literal["not_independently_verified"] = "not_independently_verified"

    _id = field_validator("source_id")(_identifier)
    _url = field_validator("canonical_url")(_public_url)
    _claims = field_validator("title", "publisher", "source_type")(_text)

    @model_validator(mode="after")
    def consistent_facts(self):
        if (canonical_source_url(self.canonical_url) != self.canonical_url
                or urlsplit(self.canonical_url).netloc.lower() != self.domain
                or (self.content_hash is None and self.content_hash_version is not None)
                or (self.published_at is not None and self.published_at.utcoffset() is None)):
            raise ValueError("Public facts require review")
        return self


class AdmissionState(Enum):
    PUBLIC = "public"
    UNREVIEWED = "unreviewed"
    PRIVATE = "private"


@dataclass(frozen=True, slots=True)
class CatalogContext:
    """Trusted authenticated context, never populated from a public request body."""
    registry_id: UUID
    application_id: str
    can_read_public: bool = False

    def __post_init__(self):
        if (not isinstance(self.registry_id, UUID) or self.registry_id.int == 0
                or type(self.application_id) is not str or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", self.application_id)
                or type(self.can_read_public) is not bool):
            raise CatalogInputError("Invalid trusted catalog context")


@dataclass(frozen=True, slots=True)
class TrustedAdmission:
    """Server decision bound to one registry/source; no metadata-based constructor."""
    registry_id: UUID
    source_id: str
    state: AdmissionState = AdmissionState.UNREVIEWED
    fingerprint_version: HashVersion | None = None
    policy_version: Literal["public-catalog-v1"] = POLICY_VERSION

    def __post_init__(self):
        if (not isinstance(self.registry_id, UUID) or self.registry_id.int == 0
                or type(self.source_id) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,35}", self.source_id)
                or not isinstance(self.state, AdmissionState)
                or self.fingerprint_version not in {None, HASH_VERSION}
                or self.policy_version != POLICY_VERSION):
            raise CatalogInputError("Invalid trusted admission")


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """Detached record mapping; do not supply ORM entities or lazy attributes."""
    record: Mapping[str, object] = field(repr=False)
    admission: TrustedAdmission

    def __post_init__(self):
        if not isinstance(self.record, Mapping) or type(self.admission) is not TrustedAdmission:
            raise CatalogInputError("Invalid detached catalog entry")


def project_public_source(entry: CatalogEntry, context: CatalogContext) -> PublicSourceFacts | None:
    """Filter admission BEFORE reading/validating facts, including malformed hidden rows."""
    if type(entry) is not CatalogEntry or type(context) is not CatalogContext:
        raise CatalogInputError("Typed trusted catalog inputs required")
    admission = entry.admission
    if (not context.can_read_public or admission.state is not AdmissionState.PUBLIC
            or admission.registry_id != context.registry_id):
        return None
    if entry.record.get("id") != admission.source_id:
        return None  # A grant for one source never authorizes a different source.
    try:
        # Never inspect arbitrary metadata, original URL, creation time or extra columns.
        return PublicSourceFacts(
            registry_id=context.registry_id, source_id=entry.record["id"],
            canonical_url=entry.record["canonical_url"], domain=entry.record["domain"],
            title=entry.record["title"], publisher=entry.record["publisher"],
            published_at=entry.record["published_at"], source_type=entry.record["source_type"],
            content_hash=entry.record["content_hash"], content_hash_version=admission.fingerprint_version,
        )
    except (KeyError, ValueError, TypeError):
        raise CatalogInputError("Admitted source facts require review") from None


ContentVerification = Literal["not_supplied", "matched", "missing", "version_unknown", "disagrees", "not_found"]


class IdentityDecision(CatalogContract):
    registry_id: UUID
    policy_version: Literal["public-catalog-v1"] = POLICY_VERSION
    query: IdentityQuery
    outcome: Literal["not_found", "url_matched", "related_content", "conflict"]
    content_verification: ContentVerification
    conflict_code: Literal["url_content_disagreement", "url_hash_identity_disagreement"] | None = None
    requires_review: bool
    source: PublicSourceFacts | None = None
    candidates: Annotated[tuple[PublicSourceFacts, ...], Field(max_length=2)] = ()

    @model_validator(mode="after")
    def coherent_decision(self):
        fingerprint = self.query.fingerprint
        if (any(s.registry_id != self.registry_id for s in self.candidates)
                or (self.source is not None and self.source.registry_id != self.registry_id)):
            raise ValueError("Registry identity mismatch")
        if (fingerprint is None) != (self.content_verification == "not_supplied"):
            raise ValueError("Content verification needs an explicit fingerprint")
        def matches(source):
            return (fingerprint is not None and source.content_hash == fingerprint.value
                    and source.content_hash_version == fingerprint.version)
        if self.outcome == "not_found":
            valid = (self.source is None and not self.candidates and self.conflict_code is None
                     and not self.requires_review and self.content_verification == ("not_found" if fingerprint else "not_supplied"))
        elif self.outcome == "url_matched":
            valid = (self.source is not None and self.source.canonical_url == self.query.canonical_url
                     and not self.candidates and self.conflict_code is None
                     and self.content_verification in {"not_supplied", "matched", "missing", "version_unknown"}
                     and self.requires_review == (self.content_verification in {"missing", "version_unknown"}))
            if valid and fingerprint is not None:
                valid = ((self.content_verification == "matched" and matches(self.source))
                         or (self.content_verification == "missing" and self.source.content_hash is None)
                         or (self.content_verification == "version_unknown" and self.source.content_hash is not None
                             and self.source.content_hash_version is None))
        elif self.outcome == "related_content":
            valid = (self.source is None and len(self.candidates) == 1 and self.conflict_code is None
                     and self.candidates[0].canonical_url != self.query.canonical_url
                     and self.requires_review and self.content_verification == "matched" and matches(self.candidates[0]))
        else:
            valid = (self.source is None and bool(self.candidates) and self.conflict_code is not None
                     and self.requires_review and self.content_verification == "disagrees"
                     and self.candidates[0].canonical_url == self.query.canonical_url)
            if valid and self.conflict_code == "url_content_disagreement":
                valid = (len(self.candidates) == 1 and self.candidates[0].content_hash is not None
                         and self.candidates[0].content_hash_version == fingerprint.version
                         and self.candidates[0].content_hash != fingerprint.value)
            elif valid:
                valid = (len(self.candidates) == 2 and self.candidates[0].source_id != self.candidates[1].source_id
                         and matches(self.candidates[1]))
        if not valid:
            raise ValueError("Inconsistent identity decision")
        return self


def decide_identity(query: IdentityQuery, context: CatalogContext, *,
                    url_match: CatalogEntry | None = None, content_match: CatalogEntry | None = None) -> IdentityDecision:
    """Decide over at most two detached lookup results. No lookup, mutation or merge."""
    if type(query) is not IdentityQuery or type(context) is not CatalogContext:
        raise CatalogInputError("Typed identity inputs required")
    # Hidden candidates cannot affect outcome, review flags, errors, candidates or counts.
    url = project_public_source(url_match, context) if url_match is not None else None
    hashed = project_public_source(content_match, context) if content_match is not None else None
    fingerprint = query.fingerprint
    if url is not None and url.canonical_url != query.canonical_url:
        raise CatalogInputError("URL lookup does not match the explicit canonical key")
    if hashed is not None and (fingerprint is None or hashed.content_hash != fingerprint.value
                              or hashed.content_hash_version != fingerprint.version):
        raise CatalogInputError("Content lookup does not match the versioned fingerprint")
    if url is not None and hashed is not None and url.source_id == hashed.source_id and url != hashed:
        raise CatalogInputError("Lookup snapshots are inconsistent")
    common = {"registry_id": context.registry_id, "query": query}
    if url is not None:
        if hashed is not None and hashed.source_id != url.source_id:
            return IdentityDecision(**common, outcome="conflict", content_verification="disagrees",
                conflict_code="url_hash_identity_disagreement", requires_review=True, candidates=(url, hashed))
        if fingerprint is None:
            check = "not_supplied"
        elif url.content_hash is None:
            check = "missing"
        elif url.content_hash_version is None:
            check = "version_unknown"
        elif url.content_hash == fingerprint.value:
            check = "matched"
        else:
            return IdentityDecision(**common, outcome="conflict", content_verification="disagrees",
                conflict_code="url_content_disagreement", requires_review=True, candidates=(url,))
        return IdentityDecision(**common, outcome="url_matched", content_verification=check,
            requires_review=check in {"missing", "version_unknown"}, source=url)
    if hashed is not None:
        if hashed.canonical_url == query.canonical_url:
            # A consistent snapshot must also expose this visible exact URL match.
            raise CatalogInputError("Exact URL match missing from lookup snapshot")
        return IdentityDecision(**common, outcome="related_content", content_verification="matched",
                                requires_review=True, candidates=(hashed,))
    return IdentityDecision(**common, outcome="not_found", requires_review=False,
                            content_verification="not_found" if fingerprint else "not_supplied")
