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

    message_type = request.headers.get(_TYPE_HEADER, "")

    # The signature check above proves the body came from Twitch (or someone who
    # knows the shared secret), but says nothing about its shape. Twitch always
    # sends well-formed JSON matching the documented schema in practice, but this
    # is the public internet entry point, so treat any parse/shape failure as a
    # client error rather than letting it surface as an unhandled 500 — which
    # would also read to Twitch's retry logic as a transient failure worth
    # retrying indefinitely, rather than a permanent one.
    try:
        payload = json.loads(body)

        if message_type == "webhook_callback_verification":
            return Response(content=payload["challenge"], media_type="text/plain")

        if message_type == "notification":
            subscription_type = payload["subscription"]["type"]
            channel_id = payload["event"]["broadcaster_user_id"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return Response(status_code=400)

    if message_type == "notification":
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
