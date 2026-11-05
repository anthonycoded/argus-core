"""add audit logs

Revision ID: dea3e0498bf6
Revises: 8bd61bcfd002
Create Date: 2026-11-05 11:04:46.963899
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "dea3e0498bf6"
down_revision: Union[str, Sequence[str], None] = "8bd61bcfd002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "core_audit_logs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("application_id", sa.String(length=36), nullable=True),
        sa.Column("api_key_id", sa.String(length=36), nullable=True),
        sa.Column("method", sa.String(length=10), nullable=False),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["application_id"],
            ["core_applications.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["api_key_id"],
            ["core_api_keys.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "ix_core_audit_logs_application_id",
        "core_audit_logs",
        ["application_id"],
    )

    op.create_index(
        "ix_core_audit_logs_api_key_id",
        "core_audit_logs",
        ["api_key_id"],
    )

    op.create_index(
        "ix_core_audit_logs_created_at",
        "core_audit_logs",
        ["created_at"],
    )

    op.create_index(
        "ix_core_audit_logs_status_code",
        "core_audit_logs",
        ["status_code"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_core_audit_logs_status_code",
        table_name="core_audit_logs",
    )

    op.drop_index(
        "ix_core_audit_logs_created_at",
        table_name="core_audit_logs",
    )

    op.drop_index(
        "ix_core_audit_logs_api_key_id",
        table_name="core_audit_logs",
    )

    op.drop_index(
        "ix_core_audit_logs_application_id",
        table_name="core_audit_logs",
    )

    op.drop_table("core_audit_logs")