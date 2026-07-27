from sqlalchemy.orm import Session

from app.models.outreach import OutreachEmail, OutreachStatus

OUTREACH_TEMPLATE_V1_SUBJECT = "Want your clips going out on TikTok, Reels, and Shorts?"
OUTREACH_TEMPLATE_V1_BODY = (
    "Hey {creator_name},\n\n"
    "We run StreamClip Co. — we turn great moments from your streams into short-form "
    "clips and publish them across TikTok, Instagram Reels, YouTube Shorts, and X, so "
    "your best content reaches people who aren't already watching you live.\n\n"
    "Nothing happens with your content unless you opt in. If you're interested, you "
    "can review the partnership terms and confirm here:\n\n"
    "{agree_url}\n\n"
    "No pressure either way — and if you ever want out, there's a one-click revoke "
    "link on that same page.\n\n"
    "Thanks,\nThe StreamClip Co. team\n"
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
    outreach_email.status = OutreachStatus.APPROVED.value
    outreach_email.approved_by = approved_by_user_id
    db.commit()
    db.refresh(outreach_email)
    return outreach_email
