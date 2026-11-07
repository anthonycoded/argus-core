
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.global_chess_base import Base


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, index=True
    )
    title: Mapped[str] = mapped_column(String(500))
    url: Mapped[str] = mapped_column(
        String(2048), unique=True, index=True
    )
    source: Mapped[str] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
    )
    relevance_score: Mapped[float] = mapped_column(Float)
    freshness_score: Mapped[float] = mapped_column(Float)
    score: Mapped[float] = mapped_column(Float, index=True)
    status: Mapped[str] = mapped_column(
        String(30), default="candidate", index=True
    )
    summary: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )
