from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.security import create_magic_link_token
from app.models.creator import Creator
from app.models.outreach import OutreachEmail, OutreachStatus
from app.services.email_sender import EmailSender, ResendEmailSender


async def send_approved_outreach_email(
    ctx: dict, outreach_email_id: int, email_sender: EmailSender | None = None
) -> None:
    settings = get_settings()
    sender = email_sender or ResendEmailSender(
        api_key=settings.resend_api_key, from_address=settings.resend_from_address
    )
    db = SessionLocal()
    try:
        outreach_email = db.get(OutreachEmail, outreach_email_id)
        if outreach_email is None or outreach_email.status != OutreachStatus.APPROVED:
            return
        creator = db.get(Creator, outreach_email.creator_id)
        if creator is None or creator.contact_email is None:
            return

        agree_url = (
            f"{settings.public_base_url}/partner/agree"
            f"?token={create_magic_link_token(creator.id, 'agree')}"
        )
        subject = outreach_email.subject.format(creator_name=creator.display_name)
        body = outreach_email.body.format(creator_name=creator.display_name, agree_url=agree_url)

        try:
            provider_message_id = sender.send(to=creator.contact_email, subject=subject, html_body=body)
        except Exception:  # noqa: BLE001 (intentional: any send failure leaves status APPROVED, no blind retry)
            return

        outreach_email.status = OutreachStatus.SENT.value
        outreach_email.sent_at = datetime.now(UTC)
        outreach_email.provider_message_id = provider_message_id
        db.commit()
    finally:
        db.close()
