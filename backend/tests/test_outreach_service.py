import pytest

from app.models.creator import Creator
from app.models.outreach import OutreachStatus
from app.services.outreach_service import (
    approve_outreach_email,
    draft_outreach_email,
    list_drafted_outreach_emails,
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
