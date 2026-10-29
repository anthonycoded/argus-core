
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ResearchSource(Base):
    __tablename__ = "research_sources"

    __table_args__ = (
        UniqueConstraint(
            "topic_id",
            "url",
            name="uq_research_sources_topic_url",
        ),
        Index("ix_research_sources_topic_id", "topic_id"),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"),
        nullable=False,
    )

    title: Mapped[str] = mapped_column(String(1000))
    url: Mapped[str] = mapped_column(String(2048))
    publisher: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    snippet: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    source_type: Mapped[str] = mapped_column(
        String(50),
        default="news",
    )
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
