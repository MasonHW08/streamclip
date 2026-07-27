from datetime import UTC, datetime

from app.services.stream_info import StreamInfo
from app.services.twitch_client import FakeTwitchClient


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
