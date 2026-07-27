import json

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.models.creator import Creator
from app.services.stream_discovery_service import reconcile_creator_stream_state
from app.services.twitch_client import TwitchAPIClient, verify_twitch_signature

router = APIRouter(prefix="/webhooks/twitch", tags=["webhooks"])

_MESSAGE_ID_HEADER = "Twitch-Eventsub-Message-Id"
_TIMESTAMP_HEADER = "Twitch-Eventsub-Message-Timestamp"
_SIGNATURE_HEADER = "Twitch-Eventsub-Message-Signature"
_TYPE_HEADER = "Twitch-Eventsub-Message-Type"


@router.post("/eventsub")
async def receive_eventsub(request: Request, db: Session = Depends(get_db)) -> Response:
    settings = get_settings()
    body = await request.body()

    message_id = request.headers.get(_MESSAGE_ID_HEADER, "")
    timestamp = request.headers.get(_TIMESTAMP_HEADER, "")
    signature = request.headers.get(_SIGNATURE_HEADER, "")
    if not verify_twitch_signature(settings.twitch_webhook_secret, message_id, timestamp, body, signature):
        return Response(status_code=403)

    payload = json.loads(body)
    message_type = request.headers.get(_TYPE_HEADER, "")

    if message_type == "webhook_callback_verification":
        return Response(content=payload["challenge"], media_type="text/plain")

    if message_type == "notification":
        subscription_type = payload["subscription"]["type"]
        channel_id = payload["event"]["broadcaster_user_id"]
        creator = (
            db.query(Creator)
            .filter(Creator.platform == "twitch", Creator.platform_channel_id == channel_id)
            .first()
        )
        if creator is None:
            return Response(status_code=200)

        client = TwitchAPIClient(
            client_id=settings.twitch_client_id, client_secret=settings.twitch_client_secret
        )
        if subscription_type == "stream.online":
            stream_info = client.get_stream_status(channel_id)
            if stream_info is not None:
                reconcile_creator_stream_state(db, creator, stream_info)
        elif subscription_type == "stream.offline":
            reconcile_creator_stream_state(db, creator, None)

    return Response(status_code=200)
