from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.global_chess_base import Base


class StoryClusterRecord(Base):
    __tablename__ = "story_clusters"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text)
    confidence: Mapped[int] = mapped_column(Integer)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )


class StoryClusterMember(Base):
    __tablename__ = "story_cluster_members"

    id: Mapped[int] = mapped_column(primary_key=True)

    cluster_id: Mapped[int] = mapped_column(
        ForeignKey("story_clusters.id", ondelete="CASCADE"),
        index=True,
    )

    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
