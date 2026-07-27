from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class CreatorStatus(StrEnum):
    PROSPECT = "prospect"
    CONTACTED = "contacted"
    APPLIED = "applied"
    AUTHORIZED = "authorized"
    DECLINED = "declined"
    REVOKED = "revoked"


class Creator(Base):
    __tablename__ = "creators"
    __table_args__ = (UniqueConstraint("platform", "platform_channel_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    platform_channel_id: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(255))
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=CreatorStatus.PROSPECT.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
