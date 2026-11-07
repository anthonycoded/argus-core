"""Add empty Core catalog identity and append-only admission storage.

Revision ID: 61c2a9e47b30
Revises: dea3e0498bf6

Frozen PostgreSQL DDL; never imports runtime models, provisions UUIDs, or adopts data.
"""
from alembic import op
import sqlalchemy as sa

revision = "61c2a9e47b30"
down_revision = "dea3e0498bf6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE core_source_catalogs (
	singleton SMALLINT NOT NULL,
	registry_id UUID NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (singleton),
	CONSTRAINT ck_core_source_catalogs_singleton CHECK (singleton = 1),
	CONSTRAINT ck_core_source_catalogs_nonzero CHECK (registry_id <> '00000000-0000-0000-0000-000000000000'),
	UNIQUE (registry_id)
)
""")
    op.execute("""
CREATE TABLE core_source_admission_events (
	event_id UUID NOT NULL,
	registry_id UUID NOT NULL,
	source_id VARCHAR(36) NOT NULL,
	decision_version BIGINT NOT NULL,
	action VARCHAR(8) NOT NULL,
	policy_version VARCHAR(48) NOT NULL,
	normalization_version VARCHAR(48),
	fingerprint_version VARCHAR(48),
	serialization_version VARCHAR(48),
	approved_facts JSONB,
	facts_digest VARCHAR(64),
	actor_issuer VARCHAR(128) NOT NULL,
	actor_subject VARCHAR(200) NOT NULL,
	reason_code VARCHAR(40) NOT NULL,
	review_reference VARCHAR(128),
	operation_id UUID NOT NULL,
	expected_version BIGINT NOT NULL,
	decided_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (event_id),
	CONSTRAINT uq_core_admission_stream_version UNIQUE (registry_id, source_id, decision_version),
	CONSTRAINT uq_core_admission_operation UNIQUE (operation_id),
	CONSTRAINT ck_core_admission_version CHECK (decision_version > 0 AND expected_version = decision_version - 1),
	CONSTRAINT ck_core_admission_action CHECK (action IN ('approve', 'reject', 'revoke')),
	CONSTRAINT ck_core_admission_policy CHECK (policy_version = 'public-catalog-v1'),
	CONSTRAINT ck_core_admission_reason CHECK ((action = 'approve' AND reason_code = 'public_facts_reviewed') OR (action = 'reject' AND reason_code = 'public_disclosure_rejected') OR (action = 'revoke' AND reason_code = 'public_access_revoked')),
	CONSTRAINT ck_core_admission_snapshot CHECK ((action = 'approve' AND approved_facts IS NOT NULL AND facts_digest IS NOT NULL AND normalization_version = 'core-url-normalizer-v1' AND normalization_version IS NOT NULL AND serialization_version = 'approved-facts-json-v1' AND serialization_version IS NOT NULL AND (fingerprint_version IS NULL OR fingerprint_version = 'core-content-sha256-v1')) OR (action <> 'approve' AND approved_facts IS NULL AND facts_digest IS NULL AND normalization_version IS NULL AND fingerprint_version IS NULL AND serialization_version IS NULL)),
	CONSTRAINT ck_core_admission_actor CHECK (length(actor_issuer) > 0 AND length(actor_subject) > 0),
	CONSTRAINT ck_core_admission_digest CHECK (facts_digest IS NULL OR facts_digest ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_core_admission_facts_object CHECK (approved_facts IS NULL OR (jsonb_typeof(approved_facts) = 'object' AND octet_length(approved_facts::text) <= 32768 AND approved_facts - ARRAY['registry_id','source_id','canonical_url','normalization_version','domain','title','publisher','published_at','source_type','content_hash','content_hash_version','claims_verification']::text[] = '{}'::jsonb)),
	FOREIGN KEY(registry_id) REFERENCES core_source_catalogs (registry_id) ON DELETE RESTRICT,
	FOREIGN KEY(source_id) REFERENCES core_sources (id) ON DELETE RESTRICT
)
""")


def downgrade() -> None:
    connection = op.get_bind()
    # Prevent provisioning/append races between the emptiness check and DROP.
    connection.execute(sa.text("LOCK TABLE core_source_catalogs, core_source_admission_events IN ACCESS EXCLUSIVE MODE"))
    populated = connection.execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM core_source_catalogs) OR EXISTS (SELECT 1 FROM core_source_admission_events)"
    )).scalar_one()
    if populated:
        raise RuntimeError("Admission storage is populated; downgrade requires separately reviewed archival/recovery.")
    op.drop_table("core_source_admission_events")
    op.drop_table("core_source_catalogs")
