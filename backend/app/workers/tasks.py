import html
import logging
from datetime import UTC, datetime

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.security import create_magic_link_token
from app.models.creator import Creator
from app.models.outreach import OutreachEmail, OutreachStatus
from app.services.email_sender import EmailSender, ResendEmailSender

logger = logging.getLogger(__name__)


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
        if outreach_email is None:
            logger.warning(
                "Skipping send: no outreach email with id %s", outreach_email_id
            )
            return
        if outreach_email.status != OutreachStatus.APPROVED:
            logger.warning(
                "Skipping send for outreach email %s: status is %r, expected 'approved'",
                outreach_email_id,
                outreach_email.status,
            )
            return
        creator = db.get(Creator, outreach_email.creator_id)
        if creator is None:
            logger.warning(
                "Skipping send for outreach email %s: creator %s not found",
                outreach_email_id,
                outreach_email.creator_id,
            )
            return
        if creator.contact_email is None:
            logger.warning(
                "Skipping send for outreach email %s: creator %s has no contact_email",
                outreach_email_id,
                creator.id,
            )
            return

        agree_url = (
            f"{settings.public_base_url}/partner/agree"
            f"?token={create_magic_link_token(creator.id, 'agree')}"
        )
        revoke_url = (
            f"{settings.public_base_url}/partner/revoke"
            f"?token={create_magic_link_token(creator.id, 'revoke')}"
        )
        # The outreach body is an HTML template; creator.display_name is
        # external, platform-supplied data and must be escaped before it's
        # interpolated into HTML (agree_url/revoke_url are app-generated,
        # not user input, so they don't need escaping).
        safe_display_name = html.escape(creator.display_name, quote=True)
        subject = outreach_email.subject.format(creator_name=creator.display_name)
        body = outreach_email.body.format(
            creator_name=safe_display_name, agree_url=agree_url, revoke_url=revoke_url
        )

        try:
            provider_message_id = sender.send(to=creator.contact_email, subject=subject, html_body=body)
        except Exception:
            # Intentional: any send failure leaves status APPROVED, no blind retry.
            # Retry deliberately via POST /internal/outreach/{id}/retry.
            logger.exception(
                "Failed to send outreach email %s to creator %s; leaving status 'approved' "
                "for manual retry",
                outreach_email_id,
                creator.id,
            )
            return

        outreach_email.status = OutreachStatus.SENT.value
        outreach_email.sent_at = datetime.now(UTC)
        outreach_email.provider_message_id = provider_message_id
        db.commit()
    finally:
        db.close()
