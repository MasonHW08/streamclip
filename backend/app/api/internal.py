from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.internal_auth import require_internal_user
from app.core.db import get_db
from app.models.user import User
from app.services.outreach_service import (
    approve_outreach_email,
    list_drafted_outreach_emails,
    retry_send_outreach_email,
)
from app.workers.queue import enqueue_send_outreach_email

router = APIRouter(prefix="/internal", tags=["internal"])


def _outreach_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    not_found = message.startswith("No outreach email with id")
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND if not_found else status.HTTP_409_CONFLICT,
        detail=message,
    )


@router.get("/outreach")
def list_outreach(
    db: Session = Depends(get_db), _user: User = Depends(require_internal_user)
) -> list[dict]:
    drafts = list_drafted_outreach_emails(db)
    return [
        {"id": d.id, "creator_id": d.creator_id, "subject": d.subject, "status": d.status}
        for d in drafts
    ]


@router.post("/outreach/{outreach_email_id}/approve")
async def approve_outreach(
    outreach_email_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_internal_user),
) -> dict:
    try:
        outreach_email = approve_outreach_email(db, outreach_email_id, approved_by_user_id=user.id)
    except ValueError as exc:
        raise _outreach_error(exc) from exc
    await enqueue_send_outreach_email(outreach_email.id)
    return {"id": outreach_email.id, "status": outreach_email.status}


@router.post("/outreach/{outreach_email_id}/retry")
async def retry_outreach(
    outreach_email_id: int,
    db: Session = Depends(get_db),
    _user: User = Depends(require_internal_user),
) -> dict:
    """Re-enqueue a send for an email stuck at 'approved' after a failed send."""
    try:
        outreach_email = retry_send_outreach_email(db, outreach_email_id)
    except ValueError as exc:
        raise _outreach_error(exc) from exc
    await enqueue_send_outreach_email(outreach_email.id)
    return {"id": outreach_email.id, "status": outreach_email.status}
