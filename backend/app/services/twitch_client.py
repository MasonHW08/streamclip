import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from app.core.config import get_settings
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
        # Every subscribe() call, in order, never deduplicated. `subscribed_channel_ids`
        # is a set, so a redundant subscribe() for an already-subscribed channel
        # is indistinguishable from no call at all when inspecting it — which
        # made redundant-subscription bugs untestable. Assert against this list
        # to prove subscribe() was (or was not) called again.
        self.subscribe_calls: list[tuple[str, str]] = []

    def get_stream_status(self, channel_id: str) -> StreamInfo | None:
        return self.stream_status.get(channel_id)

    def list_subscribed_channel_ids(self) -> set[str]:
        return set(self.subscribed_channel_ids)

    def subscribe(self, channel_id: str, callback_url: str) -> None:
        self.subscribe_calls.append((channel_id, callback_url))
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

    def _list_subscriptions(self) -> list[dict]:
        """Return every EventSub subscription, following Twitch's pagination.

        Twitch paginates GET /helix/eventsub/subscriptions and returns a
        `pagination.cursor` when more pages exist. Reading only the first page
        under-reports the subscription list, which matters at low double-digit
        creator counts (each creator holds two subscriptions: stream.online and
        stream.offline). An incomplete list makes
        reconcile_twitch_subscriptions' `authorized - subscribed` diff wrong —
        it would re-subscribe channels that are already subscribed — and makes
        unsubscribe_channel miss stale subscriptions it was asked to delete.
        """
        subscriptions: list[dict] = []
        params: dict[str, str] = {}
        while True:
            response = httpx.get(
                _EVENTSUB_URL, headers=self._headers(), params=params, timeout=10.0
            )
            response.raise_for_status()
            payload = response.json()
            subscriptions.extend(payload["data"])
            cursor = (payload.get("pagination") or {}).get("cursor")
            if not cursor:
                return subscriptions
            params = {"after": cursor}

    def list_subscribed_channel_ids(self) -> set[str]:
        # Only `enabled` subscriptions actually deliver events. Twitch keeps
        # returning dead ones (webhook_callback_verification_failed,
        # notification_failures_exceeded, authorization_revoked, ...), and
        # counting those as "subscribed" would permanently blind
        # reconcile_twitch_subscriptions to its own primary failure mode: a
        # subscription that stopped working never gets re-created.
        return {
            item["condition"]["broadcaster_user_id"]
            for item in self._list_subscriptions()
            if item.get("status") == "enabled"
        }

    def subscribe(self, channel_id: str, callback_url: str) -> None:
        settings = get_settings()
        for event_type in ("stream.online", "stream.offline"):
            response = httpx.post(
                _EVENTSUB_URL,
                headers=self._headers(),
                json={
                    "type": event_type,
                    "version": "1",
                    "condition": {"broadcaster_user_id": channel_id},
                    "transport": {
                        "method": "webhook",
                        "callback": callback_url,
                        "secret": settings.twitch_webhook_secret,
                    },
                },
                timeout=10.0,
            )
            response.raise_for_status()

    def unsubscribe_channel(self, channel_id: str) -> None:
        # Deliberately NOT filtered by status, unlike list_subscribed_channel_ids:
        # when removing a channel we want every subscription gone, including
        # the dead/failed ones.
        subscription_ids = [
            item["id"]
            for item in self._list_subscriptions()
            if item["condition"]["broadcaster_user_id"] == channel_id
        ]
        for subscription_id in subscription_ids:
            delete_response = httpx.delete(
                _EVENTSUB_URL,
                headers=self._headers(),
                params={"id": subscription_id},
                timeout=10.0,
            )
            delete_response.raise_for_status()
