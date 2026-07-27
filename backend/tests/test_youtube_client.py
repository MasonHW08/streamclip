from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from app.services.stream_info import StreamInfo
from app.services.youtube_client import FakeYouTubeClient, YouTubeAPIClient


def test_fake_client_returns_configured_stream_status():
    client = FakeYouTubeClient()
    info = StreamInfo(
        external_stream_id="v1", title="t", category=None, viewer_count=5,
        started_at=datetime.now(UTC),
    )
    client.stream_status["channel-1"] = info

    assert client.get_stream_status("channel-1") == info
    assert client.get_stream_status("channel-2") is None


@patch("app.services.youtube_client.httpx.get")
def test_get_stream_status_returns_info_when_live(mock_get):
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "items": [
            {
                "id": {"videoId": "vid-1"},
                "snippet": {
                    "title": "Live now",
                    "liveBroadcastContent": "live",
                    "publishTime": "2026-01-01T00:00:00Z",
                },
            }
        ]
    }
    mock_get.return_value = response

    client = YouTubeAPIClient(api_key="key")
    info = client.get_stream_status("channel-1")

    assert info is not None
    assert info.external_stream_id == "vid-1"
    assert info.title == "Live now"
    assert info.viewer_count == 0
    call_kwargs = mock_get.call_args.kwargs
    assert call_kwargs["params"]["channelId"] == "channel-1"
    assert call_kwargs["params"]["eventType"] == "live"
    assert call_kwargs["params"]["key"] == "key"


@patch("app.services.youtube_client.httpx.get")
def test_get_stream_status_returns_none_when_not_live(mock_get):
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"items": []}
    mock_get.return_value = response

    client = YouTubeAPIClient(api_key="key")
    assert client.get_stream_status("channel-1") is None
