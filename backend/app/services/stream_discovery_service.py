import logging
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.creator import Creator
from app.models.stream_session import StreamSession, ViewerSnapshot
from app.services.permission_gate import is_authorized
from app.services.stream_info import StreamInfo
from app.services.twitch_client import TwitchClient

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


def list_authorized_creators(db: Session, platform: str) -> list[Creator]:
    """Return all creators for a given platform who are currently authorized.

    A creator is authorized when both:
    1. Creator.status == CreatorStatus.AUTHORIZED, and
    2. at least one Agreement row has Agreement.status == AgreementStatus.ACTIVE.
    """
    creators = db.query(Creator).filter(Creator.platform == platform).all()
    return [c for c in creators if is_authorized(db, c.id)]


def reconcile_twitch_subscriptions(db: Session, client: TwitchClient, callback_url: str) -> None:
    """Reconcile Twitch EventSub subscriptions against authorized creators.

    Ensures that:
    - Every authorized Twitch creator has an active subscription
    - No stale subscriptions exist for creators who are no longer authorized
    """
    authorized_channel_ids = {
        c.platform_channel_id for c in list_authorized_creators(db, platform="twitch")
    }
    subscribed_channel_ids = client.list_subscribed_channel_ids()

    # Subscribe to newly authorized creators. Each call is isolated: Twitch
    # answers 409 Conflict when a subscription already exists (a genuine race,
    # or any other single-channel failure — 429, 5xx, a bad callback), and
    # letting that propagate would abort the whole run *including the
    # unsubscribe phase below*, so revoked creators' stale subscriptions would
    # silently stop being cleaned up too. Same per-item isolation the pollers
    # in workers/stream_discovery_tasks.py use.
    for channel_id in authorized_channel_ids - subscribed_channel_ids:
        try:
            client.subscribe(channel_id, callback_url)
        except Exception:
            logger.exception(
                "reconcile_twitch_subscriptions: failed to subscribe channel_id=%s; skipping",
                channel_id,
            )

    # Unsubscribe from no-longer-authorized creators
    for channel_id in subscribed_channel_ids - authorized_channel_ids:
        try:
            client.unsubscribe_channel(channel_id)
        except Exception:
            logger.exception(
                "reconcile_twitch_subscriptions: failed to unsubscribe channel_id=%s; skipping",
                channel_id,
            )
