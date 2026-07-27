from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.creator import Creator
from app.models.stream_session import StreamSession, ViewerSnapshot


def _make_creator(db_session):
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    return creator


def test_create_stream_session(db_session):
    creator = _make_creator(db_session)
    session = StreamSession(
        creator_id=creator.id,
        platform="twitch",
        external_stream_id="stream-1",
        title="Ranked grind",
        category="League of Legends",
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    db_session.commit()

    assert session.id is not None
    assert session.ended_at is None


def test_duplicate_platform_external_stream_id_rejected(db_session):
    creator = _make_creator(db_session)
    db_session.add(
        StreamSession(
            creator_id=creator.id, platform="twitch", external_stream_id="1",
            started_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    db_session.add(
        StreamSession(
            creator_id=creator.id, platform="twitch", external_stream_id="1",
            started_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_create_viewer_snapshot(db_session):
    creator = _make_creator(db_session)
    session = StreamSession(
        creator_id=creator.id, platform="twitch", external_stream_id="1",
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    db_session.commit()

    snapshot = ViewerSnapshot(stream_session_id=session.id, viewer_count=42)
    db_session.add(snapshot)
    db_session.commit()

    assert snapshot.id is not None
    assert snapshot.viewer_count == 42
