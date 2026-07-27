import logging
import re

import pytest

from app.core.security import verify_magic_link_token
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
async def test_send_escapes_html_in_creator_display_name(db_session, monkeypatch):
    monkeypatch.setattr("app.workers.tasks.SessionLocal", lambda: _NonClosingSessionProxy(db_session))
    creator = Creator(
        platform="twitch",
        platform_channel_id="1",
        display_name="<script>alert(1)</script>",
        contact_email="a@example.com",
    )
    db_session.add(creator)
    db_session.commit()
    outreach_email = draft_outreach_email(db_session, creator.id)
    approve_outreach_email(db_session, outreach_email.id, approved_by_user_id=1)

    fake_sender = FakeEmailSender()
    await send_approved_outreach_email({}, outreach_email.id, email_sender=fake_sender)

    sent_body = fake_sender.sent[0]["html_body"]
    assert "<script>" not in sent_body
    assert "&lt;script&gt;" in sent_body


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


@pytest.mark.asyncio
async def test_sent_email_is_html_with_paragraphs_and_links(db_session, monkeypatch):
    monkeypatch.setattr("app.workers.tasks.SessionLocal", lambda: _NonClosingSessionProxy(db_session))
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A", contact_email="a@example.com")
    db_session.add(creator)
    db_session.commit()
    outreach_email = draft_outreach_email(db_session, creator.id)
    approve_outreach_email(db_session, outreach_email.id, approved_by_user_id=1)

    fake_sender = FakeEmailSender()
    await send_approved_outreach_email({}, outreach_email.id, email_sender=fake_sender)

    body = fake_sender.sent[0]["html_body"]
    assert "{agree_url}" not in body and "{revoke_url}" not in body
    assert body.count("<p>") >= 4  # real paragraphs, not one run-on block
    agree_match = re.search(r'<a href="([^"]*/partner/agree\?token=[\w.\-]+)"', body)
    revoke_match = re.search(r'<a href="([^"]*/partner/revoke\?token=[\w.\-]+)"', body)
    assert agree_match and revoke_match
    assert verify_magic_link_token(agree_match.group(1).split("token=")[1], "agree") == creator.id
    assert verify_magic_link_token(revoke_match.group(1).split("token=")[1], "revoke") == creator.id


@pytest.mark.asyncio
async def test_missing_email_is_logged(db_session, monkeypatch, caplog):
    monkeypatch.setattr("app.workers.tasks.SessionLocal", lambda: _NonClosingSessionProxy(db_session))
    with caplog.at_level(logging.WARNING, logger="app.workers.tasks"):
        await send_approved_outreach_email({}, 999999, email_sender=FakeEmailSender())
    assert "no outreach email with id 999999" in caplog.text


@pytest.mark.asyncio
async def test_wrong_status_is_logged(db_session, monkeypatch, caplog):
    monkeypatch.setattr("app.workers.tasks.SessionLocal", lambda: _NonClosingSessionProxy(db_session))
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A", contact_email="a@example.com")
    db_session.add(creator)
    db_session.commit()
    outreach_email = draft_outreach_email(db_session, creator.id)  # still drafted

    with caplog.at_level(logging.WARNING, logger="app.workers.tasks"):
        await send_approved_outreach_email({}, outreach_email.id, email_sender=FakeEmailSender())
    assert "expected 'approved'" in caplog.text


@pytest.mark.asyncio
async def test_creator_without_contact_email_is_logged(db_session, monkeypatch, caplog):
    monkeypatch.setattr("app.workers.tasks.SessionLocal", lambda: _NonClosingSessionProxy(db_session))
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")  # no contact_email
    db_session.add(creator)
    db_session.commit()
    outreach_email = draft_outreach_email(db_session, creator.id)
    approve_outreach_email(db_session, outreach_email.id, approved_by_user_id=1)

    with caplog.at_level(logging.WARNING, logger="app.workers.tasks"):
        await send_approved_outreach_email({}, outreach_email.id, email_sender=FakeEmailSender())
    assert "has no contact_email" in caplog.text


class _ExplodingSender:
    def send(self, to: str, subject: str, html_body: str) -> str:
        raise RuntimeError("provider is down")


@pytest.mark.asyncio
async def test_send_failure_is_logged_and_leaves_status_approved(db_session, monkeypatch, caplog):
    monkeypatch.setattr("app.workers.tasks.SessionLocal", lambda: _NonClosingSessionProxy(db_session))
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A", contact_email="a@example.com")
    db_session.add(creator)
    db_session.commit()
    outreach_email = draft_outreach_email(db_session, creator.id)
    approve_outreach_email(db_session, outreach_email.id, approved_by_user_id=1)

    with caplog.at_level(logging.ERROR, logger="app.workers.tasks"):
        await send_approved_outreach_email({}, outreach_email.id, email_sender=_ExplodingSender())

    assert "Failed to send outreach email" in caplog.text
    assert "provider is down" in caplog.text  # logger.exception includes the traceback
    db_session.refresh(outreach_email)
    assert outreach_email.status == OutreachStatus.APPROVED
