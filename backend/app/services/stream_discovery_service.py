import logging
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.creator import Creator
from app.models.stream_session import StreamSession, ViewerSnapshot
from app.services.permission_gate import is_authorized
from app.services.stream_info import StreamInfo

logger = logging.getLogger(__name__)


def reconcile_creator_stream_state(
    db: Session, creator: Creator, stream_info: StreamInfo | None
) -> None:
    if not is_authorized(db, creator.id):
        return

    open_session = (
        db.query(StreamSession)
        .filter(StreamSession.creator_id == creator.id, StreamSession.ended_at.is_(None))
        .first()
    )

    if stream_info is None:
        if open_session is not None:
            open_session.ended_at = datetime.now(UTC)
            db.commit()
        return

    if open_session is not None:
        if open_session.external_stream_id == stream_info.external_stream_id:
            db.add(ViewerSnapshot(stream_session_id=open_session.id, viewer_count=stream_info.viewer_count))
            db.commit()
            return
        # Stale open session under a different stream id (e.g. a missed poll
        # between two separate broadcasts) — close it before considering the
        # new one below.
        open_session.ended_at = datetime.now(UTC)

    # A row for this (platform, external_stream_id) may already exist even
    # though it isn't the creator's currently-open session — e.g. a platform
    # glitch reports offline-then-online-again under an unchanged stream id,
    # or a caller re-delivers a stale signal for a stream that already closed.
    # The unique constraint on (platform, external_stream_id) means we cannot
    # blindly INSERT a new row in that case, so re-open the existing one
    # instead of creating a duplicate.
    existing_session = (
        db.query(StreamSession)
        .filter(
            StreamSession.creator_id == creator.id,
            StreamSession.platform == creator.platform,
            StreamSession.external_stream_id == stream_info.external_stream_id,
        )
        .first()
    )

    if existing_session is not None:
        existing_session.ended_at = None
        db.add(ViewerSnapshot(stream_session_id=existing_session.id, viewer_count=stream_info.viewer_count))
        db.commit()
        return

    session = StreamSession(
        creator_id=creator.id,
        platform=creator.platform,
        external_stream_id=stream_info.external_stream_id,
        title=stream_info.title,
        category=stream_info.category,
        started_at=stream_info.started_at,
    )
    db.add(session)
    try:
        db.commit()
    except IntegrityError:
        # Last-resort safety net: a row for this (platform, external_stream_id)
        # appeared that our lookup above didn't find under this creator_id —
        # e.g. a genuine cross-creator stream id collision, or a concurrent
        # writer racing us. This function must never crash its caller, so
        # back off and let the next poll/webhook delivery reconcile instead.
        db.rollback()
        logger.warning(
            "reconcile_creator_stream_state: IntegrityError inserting StreamSession "
            "for creator_id=%s platform=%s external_stream_id=%s; skipping",
            creator.id,
            creator.platform,
            stream_info.external_stream_id,
        )
        return

    db.add(ViewerSnapshot(stream_session_id=session.id, viewer_count=stream_info.viewer_count))
    db.commit()
