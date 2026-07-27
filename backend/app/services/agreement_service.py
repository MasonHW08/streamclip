from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.agreement import Agreement, AgreementStatus, AgreementTermsVersion
from app.models.creator import Creator, CreatorStatus


def get_active_terms_version(db: Session) -> AgreementTermsVersion:
    terms = (
        db.query(AgreementTermsVersion)
        .order_by(AgreementTermsVersion.effective_date.desc(), AgreementTermsVersion.id.desc())
        .first()
    )
    if terms is None:
        raise ValueError("No agreement terms have been seeded yet")
    return terms


def accept_agreement(
    db: Session, creator_id: int, signature_name: str, ip: str, user_agent: str
) -> Agreement:
    settings = get_settings()
    creator = db.get(Creator, creator_id)
    if creator is None:
        raise ValueError(f"No creator with id {creator_id}")
    if creator.status == CreatorStatus.AUTHORIZED.value:
        raise ValueError(f"Creator {creator_id} is already authorized")
    terms = get_active_terms_version(db)

    agreement = Agreement(
        creator_id=creator.id,
        terms_version_id=terms.id,
        rev_share_pct=settings.default_rev_share_pct,
        accepted_at=datetime.now(UTC),
        accepted_ip=ip,
        accepted_user_agent=user_agent,
        signature_name=signature_name,
        status=AgreementStatus.ACTIVE.value,
    )
    creator.status = CreatorStatus.AUTHORIZED.value
    db.add(agreement)
    db.commit()
    db.refresh(agreement)
    return agreement


def revoke_agreement(db: Session, creator_id: int) -> Agreement:
    creator = db.get(Creator, creator_id)
    if creator is None:
        raise ValueError(f"No creator with id {creator_id}")
    agreement = (
        db.query(Agreement)
        .filter(Agreement.creator_id == creator_id, Agreement.status == AgreementStatus.ACTIVE)
        .order_by(Agreement.id.desc())
        .first()
    )
    if agreement is None:
        raise ValueError(f"No active agreement for creator {creator_id}")

    agreement.status = AgreementStatus.REVOKED.value
    agreement.revoked_at = datetime.now(UTC)
    creator.status = CreatorStatus.REVOKED.value
    db.commit()
    db.refresh(agreement)
    return agreement
