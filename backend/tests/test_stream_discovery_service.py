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


def _second_authorized_creator(db_session):
    terms = AgreementTermsVersion(version="v2", effective_date=datetime.now(UTC).date(), body_markdown="x")
    db_session.add(terms)
    db_session.commit()
    creator = Creator(
        platform="twitch", platform_channel_id="2", display_name="B", status=CreatorStatus.AUTHORIZED
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


def test_stale_session_replaced_by_new_stream_id(db_session):
    """A poll is missed between two separate broadcasts: the creator's open
    session (stream-A) is still open when a NEW stream id (stream-B) shows up.
    stream-A must be closed and stream-B opened, with no crash and no
    unique-constraint violation."""
    creator = _authorized_creator(db_session)
    reconcile_creator_stream_state(db_session, creator, _info(stream_id="stream-A", viewer_count=5))
    reconcile_creator_stream_state(db_session, creator, _info(stream_id="stream-B", viewer_count=7))

    sessions = (
        db_session.query(StreamSession)
        .filter(StreamSession.creator_id == creator.id)
        .order_by(StreamSession.id)
        .all()
    )
    assert len(sessions) == 2
    session_a, session_b = sessions
    assert session_a.external_stream_id == "stream-A"
    assert session_a.ended_at is not None
    assert session_b.external_stream_id == "stream-B"
    assert session_b.ended_at is None

    snapshot_b = (
        db_session.query(ViewerSnapshot).filter(ViewerSnapshot.stream_session_id == session_b.id).first()
    )
    assert snapshot_b.viewer_count == 7


def test_same_stream_id_reused_after_close_reopens_session(db_session):
    """If the same external_stream_id shows up again after its session was
    already closed (platform glitch or a re-delivered stale signal), the
    existing row must be re-opened rather than a duplicate INSERT being
    attempted against the (platform, external_stream_id) unique constraint."""
    creator = _authorized_creator(db_session)
    reconcile_creator_stream_state(db_session, creator, _info(stream_id="stream-1", viewer_count=5))
    reconcile_creator_stream_state(db_session, creator, None)  # goes offline, session closed

    closed_session = (
        db_session.query(StreamSession)
        .filter(StreamSession.creator_id == creator.id, StreamSession.external_stream_id == "stream-1")
        .first()
    )
    assert closed_session.ended_at is not None
    closed_session_id = closed_session.id

    reconcile_creator_stream_state(db_session, creator, _info(stream_id="stream-1", viewer_count=9))

    sessions = db_session.query(StreamSession).filter(StreamSession.creator_id == creator.id).all()
    assert len(sessions) == 1  # reopened the same row, not a new one
    reopened = sessions[0]
    assert reopened.id == closed_session_id
    assert reopened.ended_at is None

    snapshots = (
        db_session.query(ViewerSnapshot)
        .filter(ViewerSnapshot.stream_session_id == reopened.id)
        .order_by(ViewerSnapshot.id)
        .all()
    )
    assert [s.viewer_count for s in snapshots] == [5, 9]


def test_stream_id_collision_across_creators_is_caught_not_crashed(db_session):
    """A pathological case: two different creators' StreamInfo report the
    SAME external_stream_id under the same platform. This should never
    legitimately happen (stream ids are per-broadcast unique), but the
    function must not raise an unhandled IntegrityError if it does."""
    creator1 = _authorized_creator(db_session)
    creator2 = _second_authorized_creator(db_session)

    reconcile_creator_stream_state(db_session, creator1, _info(stream_id="shared-id", viewer_count=5))
    reconcile_creator_stream_state(db_session, creator2, _info(stream_id="shared-id", viewer_count=8))

    creator2_sessions = db_session.query(StreamSession).filter(StreamSession.creator_id == creator2.id).all()
    assert creator2_sessions == []  # collision skipped, not inserted under creator2

    creator1_session = db_session.query(StreamSession).filter(StreamSession.creator_id == creator1.id).first()
    assert creator1_session is not None
    assert creator1_session.ended_at is None
