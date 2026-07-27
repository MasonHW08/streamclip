from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.creator import Creator
from app.models.stream_session import StreamSession, ViewerSnapshot
from app.services.permission_gate import is_authorized
from app.services.stream_info import StreamInfo


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
        open_session.ended_at = datetime.now(UTC)

    session = StreamSession(
        creator_id=creator.id,
        platform=creator.platform,
        external_stream_id=stream_info.external_stream_id,
        title=stream_info.title,
        category=stream_info.category,
        started_at=stream_info.started_at,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    db.add(ViewerSnapshot(stream_session_id=session.id, viewer_count=stream_info.viewer_count))
    db.commit()
