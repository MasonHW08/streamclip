import hashlib
import hmac
from typing import Protocol

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
