from sqlalchemy.orm import Session

from app.models.agreement import Agreement, AgreementStatus
from app.models.creator import Creator, CreatorStatus


def is_authorized(db: Session, creator_id: int) -> bool:
    creator = db.get(Creator, creator_id)
    if creator is None or creator.status != CreatorStatus.AUTHORIZED:
        return False
    active_agreement = (
        db.query(Agreement)
        .filter(Agreement.creator_id == creator_id, Agreement.status == AgreementStatus.ACTIVE)
        .first()
    )
    return active_agreement is not None
