import pytest

from app.models.creator import Creator
from app.models.outreach import OutreachStatus
from app.services.outreach_service import (
    OUTREACH_TEMPLATE_V1_BODY,
    approve_outreach_email,
    draft_outreach_email,
    list_drafted_outreach_emails,
    retry_send_outreach_email,
)


def _make_creator(db_session):
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    return creator


def test_draft_outreach_email(db_session):
    creator = _make_creator(db_session)
    outreach_email = draft_outreach_email(db_session, creator.id)
    assert outreach_email.status == OutreachStatus.DRAFTED
    assert "{creator_name}" in outreach_email.body
    assert "{agree_url}" in outreach_email.body


def test_list_drafted_only_returns_drafted(db_session):
    creator = _make_creator(db_session)
    drafted = draft_outreach_email(db_session, creator.id)
    approve_outreach_email(db_session, drafted.id, approved_by_user_id=1)
    other_draft = draft_outreach_email(db_session, creator.id)

    drafts = list_drafted_outreach_emails(db_session)

    assert [d.id for d in drafts] == [other_draft.id]


def test_approve_unknown_email_raises(db_session):
    with pytest.raises(ValueError):
        approve_outreach_email(db_session, 999999, approved_by_user_id=1)


def test_approve_already_approved_email_raises(db_session):
    creator = _make_creator(db_session)
    drafted = draft_outreach_email(db_session, creator.id)
    approve_outreach_email(db_session, drafted.id, approved_by_user_id=1)

    with pytest.raises(ValueError, match="Cannot approve outreach email .* with status 'approved'"):
        approve_outreach_email(db_session, drafted.id, approved_by_user_id=2)


def test_draft_body_is_html_with_anchor_links():
    body = OUTREACH_TEMPLATE_V1_BODY
    assert body.count("<p>") >= 4
    assert '<a href="{agree_url}">' in body
    assert '<a href="{revoke_url}">' in body


def test_draft_body_formats_cleanly():
    rendered = OUTREACH_TEMPLATE_V1_BODY.format(
        creator_name="A", agree_url="https://x/agree", revoke_url="https://x/revoke"
    )
    assert "{" not in rendered and "}" not in rendered
    assert '<a href="https://x/revoke">' in rendered


def test_retry_requires_approved_status(db_session):
    creator = _make_creator(db_session)
    drafted = draft_outreach_email(db_session, creator.id)

    with pytest.raises(ValueError, match="Cannot retry outreach email .* with status 'drafted'"):
        retry_send_outreach_email(db_session, drafted.id)


def test_retry_unknown_email_raises(db_session):
    with pytest.raises(ValueError, match="No outreach email with id"):
        retry_send_outreach_email(db_session, 999999)


def test_retry_returns_approved_email_unchanged(db_session):
    creator = _make_creator(db_session)
    drafted = draft_outreach_email(db_session, creator.id)
    approve_outreach_email(db_session, drafted.id, approved_by_user_id=1)

    retried = retry_send_outreach_email(db_session, drafted.id)

    assert retried.id == drafted.id
    assert retried.status == OutreachStatus.APPROVED
    assert retried.approved_by == 1
