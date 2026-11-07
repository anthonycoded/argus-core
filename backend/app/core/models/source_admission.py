"""Core-only durable admission storage. No implicit identity or admission defaults."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, JSON, SmallInteger, String, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models.base import CoreBase


class SourceCatalogDB(CoreBase):
    __tablename__ = "core_source_catalogs"
    singleton: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    registry_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (
        CheckConstraint("singleton = 1", name="ck_core_source_catalogs_singleton"),
        CheckConstraint("registry_id <> '00000000-0000-0000-0000-000000000000'", name="ck_core_source_catalogs_nonzero").ddl_if(dialect="postgresql"),
    )


class SourceAdmissionEventDB(CoreBase):
    __tablename__ = "core_source_admission_events"
    event_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    registry_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("core_source_catalogs.registry_id", ondelete="RESTRICT"), nullable=False)
    source_id: Mapped[str] = mapped_column(String(36), ForeignKey("core_sources.id", ondelete="RESTRICT"), nullable=False)
    decision_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(48), nullable=False)
    normalization_version: Mapped[str | None] = mapped_column(String(48))
    fingerprint_version: Mapped[str | None] = mapped_column(String(48))
    serialization_version: Mapped[str | None] = mapped_column(String(48))
    approved_facts: Mapped[dict | None] = mapped_column(JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"))
    facts_digest: Mapped[str | None] = mapped_column(String(64))
    actor_issuer: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_subject: Mapped[str] = mapped_column(String(200), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False)
    review_reference: Mapped[str | None] = mapped_column(String(128))
    operation_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    # Persist expected_version too: idempotency compares the entire original command.
    expected_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("registry_id", "source_id", "decision_version", name="uq_core_admission_stream_version"),
        UniqueConstraint("operation_id", name="uq_core_admission_operation"),
        CheckConstraint("decision_version > 0 AND expected_version = decision_version - 1", name="ck_core_admission_version"),
        CheckConstraint("action IN ('approve', 'reject', 'revoke')", name="ck_core_admission_action"),
        CheckConstraint("policy_version = 'public-catalog-v1'", name="ck_core_admission_policy"),
        CheckConstraint("(action = 'approve' AND reason_code = 'public_facts_reviewed') OR (action = 'reject' AND reason_code = 'public_disclosure_rejected') OR (action = 'revoke' AND reason_code = 'public_access_revoked')", name="ck_core_admission_reason"),
        CheckConstraint("(action = 'approve' AND approved_facts IS NOT NULL AND facts_digest IS NOT NULL AND normalization_version = 'core-url-normalizer-v1' AND normalization_version IS NOT NULL AND serialization_version = 'approved-facts-json-v1' AND serialization_version IS NOT NULL AND (fingerprint_version IS NULL OR fingerprint_version = 'core-content-sha256-v1')) OR (action <> 'approve' AND approved_facts IS NULL AND facts_digest IS NULL AND normalization_version IS NULL AND fingerprint_version IS NULL AND serialization_version IS NULL)", name="ck_core_admission_snapshot"),
        CheckConstraint("length(actor_issuer) > 0 AND length(actor_subject) > 0", name="ck_core_admission_actor"),
        CheckConstraint("facts_digest IS NULL OR facts_digest ~ '^[0-9a-f]{64}$'", name="ck_core_admission_digest").ddl_if(dialect="postgresql"),
        CheckConstraint("approved_facts IS NULL OR (jsonb_typeof(approved_facts) = 'object' AND octet_length(approved_facts::text) <= 32768 AND approved_facts - ARRAY['registry_id','source_id','canonical_url','normalization_version','domain','title','publisher','published_at','source_type','content_hash','content_hash_version','claims_verification']::text[] = '{}'::jsonb)", name="ck_core_admission_facts_object").ddl_if(dialect="postgresql"),
    )
