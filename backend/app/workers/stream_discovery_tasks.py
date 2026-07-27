import logging

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.stream_discovery_service import (
    list_authorized_creators,
    reconcile_creator_stream_state,
    reconcile_twitch_subscriptions,
)
from app.services.twitch_client import TwitchAPIClient
from app.services.youtube_client import YouTubeAPIClient

logger = logging.getLogger(__name__)


async def poll_youtube_streams(ctx: dict) -> None:
    settings = get_settings()
    client = YouTubeAPIClient(api_key=settings.youtube_api_key)
    db = SessionLocal()
    try:
        for creator in list_authorized_creators(db, platform="youtube"):
            try:
                stream_info = client.get_stream_status(creator.platform_channel_id)
                reconcile_creator_stream_state(db, creator, stream_info)
            except Exception:
                # One creator's transient failure (rate limit, 5xx, timeout,
                # deactivated channel) must not abort the rest of the batch —
                # every creator after them in iteration order would otherwise
                # go unpolled until the failure is fixed. reconcile_creator_stream_state
                # commits per-creator, so progress made before this one is kept.
                #
                # rollback() is required here, not just logging: if the failure
                # originated inside reconcile_creator_stream_state (a DB error,
                # not just the HTTP call above), the shared session's transaction
                # is left aborted. Without rolling back, the very next iteration's
                # first query against this same session would itself raise
                # (SQLAlchemy PendingRollbackError / "current transaction is
                # aborted"), which this same except would also swallow —
                # cascading one DB-origin failure into every later creator in
                # this tick appearing to fail too.
                db.rollback()
                logger.exception(
                    "poll_youtube_streams: failed to poll creator_id=%s platform_channel_id=%s; skipping",
                    creator.id,
                    creator.platform_channel_id,
                )
                continue
    finally:
        db.close()


async def poll_twitch_streams_backup(ctx: dict) -> None:
    settings = get_settings()
    client = TwitchAPIClient(
        client_id=settings.twitch_client_id, client_secret=settings.twitch_client_secret
    )
    db = SessionLocal()
    try:
        for creator in list_authorized_creators(db, platform="twitch"):
            try:
                stream_info = client.get_stream_status(creator.platform_channel_id)
                reconcile_creator_stream_state(db, creator, stream_info)
            except Exception:
                # See poll_youtube_streams above: isolate per-creator failures
                # so one bad channel doesn't stall the rest of the batch, and
                # roll back so a DB-origin failure doesn't leave the shared
                # session's transaction aborted for later creators in this tick.
                db.rollback()
                logger.exception(
                    "poll_twitch_streams_backup: failed to poll creator_id=%s platform_channel_id=%s; skipping",
                    creator.id,
                    creator.platform_channel_id,
                )
                continue
    finally:
        db.close()


async def reconcile_twitch_subscriptions_task(ctx: dict) -> None:
    settings = get_settings()
    client = TwitchAPIClient(
        client_id=settings.twitch_client_id, client_secret=settings.twitch_client_secret
    )
    db = SessionLocal()
    try:
        reconcile_twitch_subscriptions(db, client, callback_url=settings.twitch_eventsub_callback_url)
    finally:
        db.close()
