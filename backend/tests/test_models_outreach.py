from app.models.creator import Creator
from app.models.outreach import OutreachEmail, OutreachStatus


def test_create_outreach_email(db_session):
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()

    outreach_email = OutreachEmail(
        creator_id=creator.id,
        template_version="v1",
        subject="subject",
        body="body",
    )
    db_session.add(outreach_email)
    db_session.commit()

    assert outreach_email.id is not None
    assert outreach_email.status == OutreachStatus.DRAFTED
