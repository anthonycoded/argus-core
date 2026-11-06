
"""add unique content hash index

Revision ID: 8d3f39d3a946
Revises: 3f1a9c2d7e60
Create Date: 2026-11-04
"""

from alembic import op
import sqlalchemy as sa

revision = "8d3f39d3a946"
down_revision = "3f1a9c2d7e60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index(
        "ix_core_sources_content_hash",
        table_name="core_sources",
    )

    op.create_index(
        "uq_core_sources_content_hash",
        "core_sources",
        ["content_hash"],
        unique=True,
        postgresql_where=sa.text("content_hash IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_core_sources_content_hash",
        table_name="core_sources",
        postgresql_where=sa.text("content_hash IS NOT NULL"),
    )

    op.create_index(
        "ix_core_sources_content_hash",
        "core_sources",
        ["content_hash"],
        unique=False,
    )
