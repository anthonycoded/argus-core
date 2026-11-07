
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.global_chess_base import Base


class TopicAnalysis(Base):
    __tablename__ = "topic_analyses"

    id: Mapped[int] = mapped_column(primary_key=True)

    topic_id: Mapped[int] = mapped_column(
        ForeignKey("topics.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )

    summary: Mapped[str] = mapped_column(Text)
    relevance: Mapped[int] = mapped_column(Integer)
    research_priority: Mapped[int] = mapped_column(Integer)
    rationale: Mapped[str] = mapped_column(Text)
    research_questions: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(String(100))

    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
