from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.models.base import CoreBase


class AuditLogDB(CoreBase):
    __tablename__ = "core_audit_logs"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid4()),
    )

    application_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("core_applications.id", ondelete="SET NULL"),
        nullable=True,
    )

    api_key_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("core_api_keys.id", ondelete="SET NULL"),
        nullable=True,
    )

    method: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
    )

    path: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )

    status_code: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    duration_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("ix_core_audit_logs_application_id", "application_id"),
        Index("ix_core_audit_logs_api_key_id", "api_key_id"),
        Index("ix_core_audit_logs_created_at", "created_at"),
        Index("ix_core_audit_logs_status_code", "status_code"),
    )