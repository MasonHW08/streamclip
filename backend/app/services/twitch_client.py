import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from app.services.stream_info import StreamInfo


def verify_twitch_signature(
    secret: str, message_id: str, timestamp: str, body: bytes, signature_header: str
) -> bool:
    if not signature_header.startswith("sha256="):
        return False
    provided_digest = signature_header.removeprefix("sha256=")
    # A SHA-256 hex digest is always 64 lowercase hex characters. Reject anything
    # else up front so hmac.compare_digest never sees non-ASCII input, which it
    # raises TypeError on (it only accepts str objects with ASCII-only content).
    if len(provided_digest) != 64 or not all(c in "0123456789abcdef" for c in provided_digest):
        return False
    expected_digest = hmac.new(
        secret.encode(), message_id.encode() + timestamp.encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_digest, provided_digest)


class TwitchClient(Protocol):
    def get_stream_status(self, channel_id: str) -> StreamInfo | None: ...
    def list_subscribed_channel_ids(self) -> set[str]: ...
    def subscribe(self, channel_id: str, callback_url: str) -> None: ...
    def unsubscribe_channel(self, channel_id: str) -> None: ...


class FakeTwitchClient:
    def __init__(self) -> None:
        self.stream_status: dict[str, StreamInfo] = {}
        self.subscribed_channel_ids: set[str] = set()

    def get_stream_status(self, channel_id: str) -> StreamInfo | None:
        return self.stream_status.get(channel_id)

    def list_subscribed_channel_ids(self) -> set[str]:
        return set(self.subscribed_channel_ids)

    def subscribe(self, channel_id: str, callback_url: str) -> None:
        self.subscribed_channel_ids.add(channel_id)

    def unsubscribe_channel(self, channel_id: str) -> None:
        self.subscribed_channel_ids.discard(channel_id)


_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_STREAMS_URL = "https://api.twitch.tv/helix/streams"
_EVENTSUB_URL = "https://api.twitch.tv/helix/eventsub/subscriptions"


class TwitchAPIClient:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._app_token: str | None = None
        self._app_token_expires_at: datetime | None = None

    def _get_app_token(self) -> str:
        if (
            self._app_token is not None
            and self._app_token_expires_at is not None
            and datetime.now(UTC) < self._app_token_expires_at
        ):
            return self._app_token

        response = httpx.post(
            _TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant_type": "client_credentials",
            },
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
        self._app_token = payload["access_token"]
        self._app_token_expires_at = datetime.now(UTC) + timedelta(
            seconds=payload["expires_in"] - 60
        )
        return self._app_token

    def _headers(self) -> dict[str, str]:
        return {
            "Client-Id": self._client_id,
            "Authorization": f"Bearer {self._get_app_token()}",
        }

    def get_stream_status(self, channel_id: str) -> StreamInfo | None:
        response = httpx.get(
            _STREAMS_URL,
            params={"user_id": channel_id},
            headers=self._headers(),
            timeout=10.0,
        )
        response.raise_for_status()
        data = response.json()["data"]
        if not data:
            return None
        stream = data[0]
        return StreamInfo(
            external_stream_id=stream["id"],
            title=stream.get("title") or None,
            category=stream.get("game_name") or None,
            viewer_count=stream["viewer_count"],
            started_at=datetime.fromisoformat(stream["started_at"].replace("Z", "+00:00")),
        )
