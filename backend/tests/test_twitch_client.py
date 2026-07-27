import hashlib
import hmac
from datetime import UTC, datetime

from app.services.stream_info import StreamInfo
from app.services.twitch_client import FakeTwitchClient, verify_twitch_signature


def test_fake_client_returns_configured_stream_status():
    client = FakeTwitchClient()
    info = StreamInfo(
        external_stream_id="s1", title="t", category="c", viewer_count=10,
        started_at=datetime.now(UTC),
    )
    client.stream_status["channel-1"] = info

    assert client.get_stream_status("channel-1") == info
    assert client.get_stream_status("channel-2") is None


def test_fake_client_subscribe_and_unsubscribe():
    client = FakeTwitchClient()
    client.subscribe("channel-1", "https://example.com/webhook")
    client.subscribe("channel-2", "https://example.com/webhook")

    assert client.list_subscribed_channel_ids() == {"channel-1", "channel-2"}

    client.unsubscribe_channel("channel-1")

    assert client.list_subscribed_channel_ids() == {"channel-2"}


def _sign(secret: str, message_id: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(
        secret.encode(), (message_id + timestamp).encode() + body, hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


def test_verify_twitch_signature_accepts_valid_signature():
    secret, message_id, timestamp, body = "shh", "msg-1", "2026-01-01T00:00:00Z", b'{"a":1}'
    signature = _sign(secret, message_id, timestamp, body)

    assert verify_twitch_signature(secret, message_id, timestamp, body, signature) is True


def test_verify_twitch_signature_rejects_wrong_secret():
    message_id, timestamp, body = "msg-1", "2026-01-01T00:00:00Z", b'{"a":1}'
    signature = _sign("shh", message_id, timestamp, body)

    assert verify_twitch_signature("wrong-secret", message_id, timestamp, body, signature) is False


def test_verify_twitch_signature_rejects_tampered_body():
    secret, message_id, timestamp = "shh", "msg-1", "2026-01-01T00:00:00Z"
    signature = _sign(secret, message_id, timestamp, b'{"a":1}')

    assert verify_twitch_signature(secret, message_id, timestamp, b'{"a":2}', signature) is False


def test_verify_twitch_signature_rejects_malformed_header():
    assert verify_twitch_signature("shh", "msg-1", "ts", b"{}", "not-a-valid-header") is False


def test_verify_twitch_signature_rejects_non_ascii_digest_without_raising():
    signature = "sha256=" + "a" * 63 + "é"

    assert verify_twitch_signature("shh", "msg-1", "ts", b"{}", signature) is False
