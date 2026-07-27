from sqlalchemy.orm import Session

from app.models.agreement import Agreement, AgreementStatus
from app.models.creator import Creator, CreatorStatus


def is_authorized(db: Session, creator_id: int) -> bool:
    """Return True iff the creator is currently authorized to have their
    stream clipped/published.

    This is the hard gate every sub-project that touches a creator's
    livestream (stream discovery, clip detection, video processing,
    publishing) MUST call before doing anything with that creator's content.

    True only when BOTH hold:
      1. Creator.status == CreatorStatus.AUTHORIZED, and
      2. at least one Agreement row for this creator has
         Agreement.status == AgreementStatus.ACTIVE.

    A creator's history is not considered beyond these two live conditions:
    an old revoked Agreement does not block a subsequent active one, and an
    active Agreement does not help if the Creator row itself isn't AUTHORIZED
    (e.g. REVOKED overrides regardless of any Agreement rows left ACTIVE).

    This must always be a live DB check — never cached by this function.
    Callers are responsible for passing a `db` Session that cannot return a
    stale `Creator` object for this id: use a fresh per-request session, or
    call `db.expire(creator)` / `db.refresh(creator)` first if reusing a
    session that may have already loaded this Creator earlier (e.g. before a
    concurrent revocation). SQLAlchemy's `Session.get()` consults the
    session's identity map before hitting the database, so a long-lived or
    reused session could otherwise return an in-memory object that predates
    a revocation made by another session.
    """
    creator = db.get(Creator, creator_id)
    if creator is None or creator.status != CreatorStatus.AUTHORIZED:
        return False
    active_agreement = (
        db.query(Agreement)
        .filter(Agreement.creator_id == creator_id, Agreement.status == AgreementStatus.ACTIVE)
        .first()
    )
    return active_agreement is not None
