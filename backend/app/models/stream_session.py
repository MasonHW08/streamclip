from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StreamSession(Base):
    __tablename__ = "stream_sessions"
    __table_args__ = (UniqueConstraint("platform", "external_stream_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    creator_id: Mapped[int] = mapped_column(ForeignKey("creators.id"))
    platform: Mapped[str] = mapped_column(String(32))
    external_stream_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ViewerSnapshot(Base):
    __tablename__ = "viewer_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    stream_session_id: Mapped[int] = mapped_column(ForeignKey("stream_sessions.id"))
    viewer_count: Mapped[int] = mapped_column(Integer)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
