import pytest

from app.models.creator import Creator
from app.models.outreach import OutreachStatus
from app.services.email_sender import FakeEmailSender
from app.services.outreach_service import approve_outreach_email, draft_outreach_email
from app.workers.tasks import send_approved_outreach_email


class _NonClosingSessionProxy:
    """Proxies to a shared test session but no-ops `close()`.

    The worker's `finally: db.close()` is correct for production, where
    `SessionLocal()` opens a fresh session per job. In tests we monkeypatch
    `SessionLocal` to hand back the shared, transactional `db_session`
    fixture instead — a real `close()` on that session would expunge its
    objects and break the fixture for the rest of the test (and its own
    teardown). This wrapper lets the worker's real commit/get/query code
    path run unmodified while leaving fixture lifecycle to the fixture.
    """

    def __init__(self, session):
        self._session = session

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._session, name)


@pytest.mark.asyncio
async def test_send_approved_email_marks_sent(db_session, monkeypatch):
    monkeypatch.setattr("app.workers.tasks.SessionLocal", lambda: _NonClosingSessionProxy(db_session))
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A", contact_email="a@example.com")
    db_session.add(creator)
    db_session.commit()
    outreach_email = draft_outreach_email(db_session, creator.id)
    approve_outreach_email(db_session, outreach_email.id, approved_by_user_id=1)

    fake_sender = FakeEmailSender()
    await send_approved_outreach_email({}, outreach_email.id, email_sender=fake_sender)

    db_session.refresh(outreach_email)
    assert outreach_email.status == OutreachStatus.SENT
    assert outreach_email.provider_message_id == fake_sender.sent[0]["id"]
    assert "a@example.com" == fake_sender.sent[0]["to"]
    assert "{agree_url}" not in fake_sender.sent[0]["html_body"]


@pytest.mark.asyncio
async def test_skips_email_not_in_approved_state(db_session, monkeypatch):
    monkeypatch.setattr("app.workers.tasks.SessionLocal", lambda: _NonClosingSessionProxy(db_session))
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A", contact_email="a@example.com")
    db_session.add(creator)
    db_session.commit()
    outreach_email = draft_outreach_email(db_session, creator.id)  # still "drafted", not approved

    fake_sender = FakeEmailSender()
    await send_approved_outreach_email({}, outreach_email.id, email_sender=fake_sender)

    db_session.refresh(outreach_email)
    assert outreach_email.status == OutreachStatus.DRAFTED
    assert fake_sender.sent == []
