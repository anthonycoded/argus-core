"""Create the historical source registry required by the original index revision.

Revision ID: 3f1a9c2d7e60
Revises:

This intentionally creates the OLD nonunique content-hash index. Revision
8d3f39d3a946 replaces it with the current unique partial index.
"""

from alembic import op
import sqlalchemy as sa

revision = "3f1a9c2d7e60"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "core_sources",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(1000), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("domain", sa.String(255), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("publisher", sa.String(500), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_type", sa.String(100), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_url", name="uq_core_sources_canonical_url"),
    )
    op.create_index("ix_core_sources_content_hash", "core_sources", ["content_hash"], unique=False)
    op.create_index("ix_core_sources_domain", "core_sources", ["domain"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_core_sources_domain", table_name="core_sources")
    op.drop_index("ix_core_sources_content_hash", table_name="core_sources")
    op.drop_table("core_sources")
