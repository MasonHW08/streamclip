from datetime import UTC, datetime

from app.models.agreement import Agreement, AgreementStatus, AgreementTermsVersion
from app.models.creator import Creator, CreatorStatus
from app.models.stream_session import StreamSession, ViewerSnapshot
from app.services.stream_discovery_service import reconcile_creator_stream_state
from app.services.stream_info import StreamInfo


def _authorized_creator(db_session):
    terms = AgreementTermsVersion(version="v1", effective_date=datetime.now(UTC).date(), body_markdown="x")
    db_session.add(terms)
    db_session.commit()
    creator = Creator(
        platform="twitch", platform_channel_id="1", display_name="A", status=CreatorStatus.AUTHORIZED
    )
    db_session.add(creator)
    db_session.commit()
    db_session.add(
        Agreement(creator_id=creator.id, terms_version_id=terms.id, rev_share_pct=50.0, status=AgreementStatus.ACTIVE)
    )
    db_session.commit()
    return creator


def _info(stream_id="stream-1", viewer_count=10):
    return StreamInfo(
        external_stream_id=stream_id, title="t", category="c", viewer_count=viewer_count,
        started_at=datetime.now(UTC),
    )


def test_not_live_to_live_opens_session(db_session):
    creator = _authorized_creator(db_session)
    reconcile_creator_stream_state(db_session, creator, _info())

    session = db_session.query(StreamSession).filter(StreamSession.creator_id == creator.id).first()
    assert session is not None
    assert session.ended_at is None
    snapshot = db_session.query(ViewerSnapshot).filter(ViewerSnapshot.stream_session_id == session.id).first()
    assert snapshot.viewer_count == 10


def test_live_to_live_adds_snapshot_not_new_session(db_session):
    creator = _authorized_creator(db_session)
    reconcile_creator_stream_state(db_session, creator, _info(viewer_count=10))
    reconcile_creator_stream_state(db_session, creator, _info(viewer_count=20))

    sessions = db_session.query(StreamSession).filter(StreamSession.creator_id == creator.id).all()
    assert len(sessions) == 1
    snapshots = db_session.query(ViewerSnapshot).filter(ViewerSnapshot.stream_session_id == sessions[0].id).all()
    assert [s.viewer_count for s in snapshots] == [10, 20]


def test_live_to_not_live_closes_session(db_session):
    creator = _authorized_creator(db_session)
    reconcile_creator_stream_state(db_session, creator, _info())
    reconcile_creator_stream_state(db_session, creator, None)

    session = db_session.query(StreamSession).filter(StreamSession.creator_id == creator.id).first()
    assert session.ended_at is not None


def test_unauthorized_creator_is_ignored(db_session):
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()

    reconcile_creator_stream_state(db_session, creator, _info())

    assert db_session.query(StreamSession).filter(StreamSession.creator_id == creator.id).count() == 0
