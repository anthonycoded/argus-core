"""Internal admission commands and immutable snapshots; no authority issuer or I/O."""

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.core.source_catalog import CatalogContract, PublicSourceFacts, POLICY_VERSION, URL_VERSION, HASH_VERSION

SERIALIZATION_VERSION = "approved-facts-json-v1"
MAX_FACTS_BYTES = 16 * 1024
Action = Literal["approve", "reject", "revoke"]
Reason = Literal["public_facts_reviewed", "public_disclosure_rejected", "public_access_revoked"]
REASONS = {"approve": "public_facts_reviewed", "reject": "public_disclosure_rejected", "revoke": "public_access_revoked"}


class AdmissionError(ValueError):
    """Constant diagnostics, without source facts or database exception details."""


def nonzero(value: UUID) -> UUID:
    if value.int == 0:
        raise ValueError("Nonzero registry UUID required")
    return value


def canonical_facts(facts: PublicSourceFacts) -> bytes:
    """V1: UTF-8, sorted keys, explicit nulls, UTC six-digit timestamps, no NFC."""
    if type(facts) is not PublicSourceFacts:
        raise AdmissionError("Strict public facts required")
    try:
        facts = PublicSourceFacts.model_validate(facts.model_dump())
        nonzero(facts.registry_id)
        data = facts.model_dump(mode="json")
        if facts.published_at is not None:
            data["published_at"] = facts.published_at.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        raw = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_FACTS_BYTES:
            raise ValueError
        return raw
    except (ValueError, TypeError, UnicodeError):
        raise AdmissionError("Invalid approved facts") from None


def facts_digest(facts: PublicSourceFacts) -> str:
    return hashlib.sha256((SERIALIZATION_VERSION + "\n").encode("ascii") + canonical_facts(facts)).hexdigest()


def stored_facts(value: dict) -> PublicSourceFacts:
    """Strict JSON-mode UUID/date decoding, followed by canonical validation."""
    try:
        raw = json.dumps(value, allow_nan=False, ensure_ascii=False)
        # A coarse guard before schema parsing; exact canonical bytes are checked below.
        if len(raw.encode("utf-8")) > 32 * 1024:
            raise ValueError
        facts = PublicSourceFacts.model_validate_json(raw)
        canonical_facts(facts)
        return facts
    except (ValueError, TypeError, UnicodeError):
        raise AdmissionError("Invalid approved facts") from None


class TrustedAdmissionExecution(CatalogContract):
    """A trusted caller assertion, NOT authentication or a provisioned capability.

    No existing application creates this context. A future authority adapter and
    restricted database credentials are required before exposing write execution.
    """
    registry_id: UUID
    actor_issuer: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    actor_subject: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    _registry = field_validator("registry_id")(nonzero)


class AdmissionCommand(CatalogContract):
    operation_id: UUID
    source_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,35}$")
    expected_version: int = Field(ge=0, lt=9223372036854775807)
    action: Action
    reason_code: Reason
    policy_version: Literal["public-catalog-v1"] = POLICY_VERSION
    normalization_version: Literal["core-url-normalizer-v1"] | None = None
    fingerprint_version: Literal["core-content-sha256-v1"] | None = None
    serialization_version: Literal["approved-facts-json-v1"] | None = None
    approved_facts: PublicSourceFacts | None = Field(default=None, repr=False)
    facts_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    review_reference: str | None = Field(default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

    @model_validator(mode="after")
    def coherent(self):
        if self.reason_code != REASONS[self.action]:
            raise ValueError("Reason does not match action")
        if self.action == "approve":
            facts = self.approved_facts
            if (facts is None or facts.source_id != self.source_id
                    or self.normalization_version != URL_VERSION
                    or self.serialization_version != SERIALIZATION_VERSION
                    or self.fingerprint_version != facts.content_hash_version
                    or self.facts_digest != facts_digest(facts)):
                raise ValueError("Invalid approval snapshot")
        elif any(v is not None for v in (self.approved_facts, self.facts_digest,
                self.normalization_version, self.fingerprint_version, self.serialization_version)):
            raise ValueError("Non-approval cannot carry approved facts")
        return self


class AdmissionEvent(CatalogContract):
    event_id: UUID
    registry_id: UUID
    decision_version: int
    actor_issuer: str
    actor_subject: str
    decided_at: datetime
    command: AdmissionCommand

    @model_validator(mode="after")
    def bound_snapshot(self):
        if (self.registry_id.int == 0 or self.decision_version != self.command.expected_version + 1
                or (self.command.approved_facts is not None
                    and self.command.approved_facts.registry_id != self.registry_id)):
            raise ValueError("Inconsistent stored admission event")
        return self
