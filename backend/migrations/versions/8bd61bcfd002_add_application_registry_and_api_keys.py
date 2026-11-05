"""add application registry and api keys

Revision ID: 8bd61bcfd002
Revises: 8d3f39d3a946
"""

from alembic import op
import sqlalchemy as sa


revision = "8bd61bcfd002"
down_revision = "8d3f39d3a946"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "core_applications",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),

    )

    op.create_index(
        "ix_core_applications_slug",
        "core_applications",
        ["slug"],
        unique=True,
    )

    op.create_table(
        "core_api_keys",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["core_applications.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )

    op.create_index(
        "ix_core_api_keys_application_id",
        "core_api_keys",
        ["application_id"],
        unique=False,
    )

    op.create_index(
        "ix_core_api_keys_key_prefix",
        "core_api_keys",
        ["key_prefix"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_core_api_keys_key_prefix",
        table_name="core_api_keys",
    )
    op.drop_index(
        "ix_core_api_keys_application_id",
        table_name="core_api_keys",
    )
    op.drop_table("core_api_keys")

    op.drop_index(
        "ix_core_applications_slug",
        table_name="core_applications",
    )
    op.drop_table("core_applications")