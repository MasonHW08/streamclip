from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.stream_discovery_service import (
    list_authorized_creators,
    reconcile_creator_stream_state,
    reconcile_twitch_subscriptions,
)
from app.services.twitch_client import TwitchAPIClient
from app.services.youtube_client import YouTubeAPIClient


async def poll_youtube_streams(ctx: dict) -> None:
    settings = get_settings()
    client = YouTubeAPIClient(api_key=settings.youtube_api_key)
    db = SessionLocal()
    try:
        for creator in list_authorized_creators(db, platform="youtube"):
            stream_info = client.get_stream_status(creator.platform_channel_id)
            reconcile_creator_stream_state(db, creator, stream_info)
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
            stream_info = client.get_stream_status(creator.platform_channel_id)
            reconcile_creator_stream_state(db, creator, stream_info)
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
