from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import Date, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AgreementStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    REVOKED = "revoked"


class AgreementTermsVersion(Base):
    __tablename__ = "agreement_terms_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(32), unique=True)
    effective_date: Mapped[date] = mapped_column(Date)
    body_markdown: Mapped[str] = mapped_column(Text)


class Agreement(Base):
    __tablename__ = "agreements"

    id: Mapped[int] = mapped_column(primary_key=True)
    creator_id: Mapped[int] = mapped_column(ForeignKey("creators.id"))
    terms_version_id: Mapped[int] = mapped_column(ForeignKey("agreement_terms_versions.id"))
    rev_share_pct: Mapped[float] = mapped_column(Float)
    scope_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    accepted_user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    signature_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=AgreementStatus.PENDING.value)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
