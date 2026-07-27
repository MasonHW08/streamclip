import pytest
from sqlalchemy.exc import IntegrityError

from app.models.creator import Creator, CreatorStatus


def test_create_creator(db_session):
    creator = Creator(
        platform="twitch",
        platform_channel_id="123",
        display_name="Some Streamer",
        contact_email="streamer@example.com",
    )
    db_session.add(creator)
    db_session.commit()

    assert creator.id is not None
    assert creator.status == CreatorStatus.PROSPECT


def test_duplicate_platform_channel_id_rejected(db_session):
    db_session.add(Creator(platform="twitch", platform_channel_id="1", display_name="A"))
    db_session.commit()

    db_session.add(Creator(platform="twitch", platform_channel_id="1", display_name="B"))
    with pytest.raises(IntegrityError):
        db_session.commit()
