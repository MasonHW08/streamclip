from sqlalchemy.orm import Session

from app.models.outreach import OutreachEmail, OutreachStatus

OUTREACH_TEMPLATE_V1_SUBJECT = "Want your clips going out on TikTok, Reels, and Shorts?"
OUTREACH_TEMPLATE_V1_BODY = (
    "<p>Hey {creator_name},</p>\n"
    "<p>We run StreamClip Co. — we turn great moments from your streams into short-form "
    "clips and publish them across TikTok, Instagram Reels, YouTube Shorts, and X, so "
    "your best content reaches people who aren't already watching you live.</p>\n"
    "<p>Nothing happens with your content unless you opt in. If you're interested, you "
    'can <a href="{agree_url}">review the partnership terms and confirm here</a>.</p>\n'
    "<p>No pressure either way — and if you ever want out, there's a one-click "
    '<a href="{revoke_url}">revoke link</a>, also linked from that same page.</p>\n'
    "<p>Thanks,<br>The StreamClip Co. team</p>\n"
)


def draft_outreach_email(db: Session, creator_id: int, template_version: str = "v1") -> OutreachEmail:
    outreach_email = OutreachEmail(
        creator_id=creator_id,
        template_version=template_version,
        subject=OUTREACH_TEMPLATE_V1_SUBJECT,
        body=OUTREACH_TEMPLATE_V1_BODY,
        status=OutreachStatus.DRAFTED.value,
    )
    db.add(outreach_email)
    db.commit()
    db.refresh(outreach_email)
    return outreach_email


def list_drafted_outreach_emails(db: Session) -> list[OutreachEmail]:
    return (
        db.query(OutreachEmail)
        .filter(OutreachEmail.status == OutreachStatus.DRAFTED)
        .order_by(OutreachEmail.id)
        .all()
    )


def approve_outreach_email(db: Session, outreach_email_id: int, approved_by_user_id: int) -> OutreachEmail:
    outreach_email = db.get(OutreachEmail, outreach_email_id)
    if outreach_email is None:
        raise ValueError(f"No outreach email with id {outreach_email_id}")
    if outreach_email.status != OutreachStatus.DRAFTED.value:
        raise ValueError(
            f"Cannot approve outreach email {outreach_email_id} with status {outreach_email.status!r} (expected 'drafted')"
        )
    outreach_email.status = OutreachStatus.APPROVED.value
    outreach_email.approved_by = approved_by_user_id
    db.commit()
    db.refresh(outreach_email)
    return outreach_email


def retry_send_outreach_email(db: Session, outreach_email_id: int) -> OutreachEmail:
    """Re-confirm an already-approved email so its send can be re-enqueued.

    A failed send leaves the row at APPROVED, and `approve_outreach_email` only
    accepts DRAFTED — so without this there is no way to re-trigger a send. The
    row is returned unchanged; the caller re-enqueues the job.
    """
    outreach_email = db.get(OutreachEmail, outreach_email_id)
    if outreach_email is None:
        raise ValueError(f"No outreach email with id {outreach_email_id}")
    if outreach_email.status != OutreachStatus.APPROVED.value:
        raise ValueError(
            f"Cannot retry outreach email {outreach_email_id} with status "
            f"{outreach_email.status!r} (expected 'approved')"
        )
    return outreach_email
