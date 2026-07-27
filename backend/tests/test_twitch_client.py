import hashlib
import hmac
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from app.services.stream_info import StreamInfo
from app.services.twitch_client import FakeTwitchClient, TwitchAPIClient, verify_twitch_signature


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


def _mock_response(json_data, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data
    response.raise_for_status = MagicMock()
    return response


@patch("app.services.twitch_client.httpx.post")
@patch("app.services.twitch_client.httpx.get")
def test_get_stream_status_fetches_and_caches_token(mock_get, mock_post):
    mock_post.return_value = _mock_response({"access_token": "tok-1", "expires_in": 3600})
    mock_get.return_value = _mock_response(
        {
            "data": [
                {
                    "id": "stream-1",
                    "title": "Ranked grind",
                    "game_name": "League of Legends",
                    "viewer_count": 100,
                    "started_at": "2026-01-01T00:00:00Z",
                }
            ]
        }
    )

    client = TwitchAPIClient(client_id="cid", client_secret="csecret")
    info = client.get_stream_status("channel-1")

    assert info is not None
    assert info.external_stream_id == "stream-1"
    assert info.title == "Ranked grind"
    assert info.category == "League of Legends"
    assert info.viewer_count == 100
    assert info.started_at == datetime(2026, 1, 1, tzinfo=UTC)
    assert mock_post.call_count == 1  # token fetched once

    client.get_stream_status("channel-1")
    assert mock_post.call_count == 1  # second call reuses cached token, no new fetch


@patch("app.services.twitch_client.httpx.post")
@patch("app.services.twitch_client.httpx.get")
def test_get_stream_status_returns_none_when_not_live(mock_get, mock_post):
    mock_post.return_value = _mock_response({"access_token": "tok-1", "expires_in": 3600})
    mock_get.return_value = _mock_response({"data": []})

    client = TwitchAPIClient(client_id="cid", client_secret="csecret")
    assert client.get_stream_status("channel-1") is None


@patch("app.services.twitch_client.httpx.delete")
@patch("app.services.twitch_client.httpx.post")
@patch("app.services.twitch_client.httpx.get")
def test_list_subscribed_channel_ids(mock_get, mock_post, mock_delete, monkeypatch):
    monkeypatch.setenv("TWITCH_WEBHOOK_SECRET", "shh")
    from app.core.config import get_settings

    get_settings.cache_clear()
    mock_post.return_value = _mock_response({"access_token": "tok-1", "expires_in": 3600})
    mock_get.return_value = _mock_response(
        {
            "data": [
                {"condition": {"broadcaster_user_id": "channel-1"}},
                {"condition": {"broadcaster_user_id": "channel-2"}},
                {"condition": {"broadcaster_user_id": "channel-1"}},
            ]
        }
    )

    client = TwitchAPIClient(client_id="cid", client_secret="csecret")
    assert client.list_subscribed_channel_ids() == {"channel-1", "channel-2"}


@patch("app.services.twitch_client.httpx.delete")
@patch("app.services.twitch_client.httpx.post")
@patch("app.services.twitch_client.httpx.get")
def test_subscribe_creates_online_and_offline_subscriptions(
    mock_get, mock_post, mock_delete, monkeypatch
):
    monkeypatch.setenv("TWITCH_WEBHOOK_SECRET", "shh")
    from app.core.config import get_settings

    get_settings.cache_clear()
    token_response = _mock_response({"access_token": "tok-1", "expires_in": 3600})
    subscribe_response = _mock_response({}, status_code=202)
    mock_post.side_effect = [token_response, subscribe_response, subscribe_response]

    client = TwitchAPIClient(client_id="cid", client_secret="csecret")
    client.subscribe("channel-1", "https://example.com/webhooks/twitch/eventsub")

    subscription_calls = mock_post.call_args_list[1:]
    assert len(subscription_calls) == 2
    event_types = {call.kwargs["json"]["type"] for call in subscription_calls}
    assert event_types == {"stream.online", "stream.offline"}
    for call in subscription_calls:
        assert call.kwargs["json"]["condition"]["broadcaster_user_id"] == "channel-1"
        assert call.kwargs["json"]["transport"]["secret"] == "shh"


@patch("app.services.twitch_client.httpx.delete")
@patch("app.services.twitch_client.httpx.post")
@patch("app.services.twitch_client.httpx.get")
def test_unsubscribe_channel_deletes_its_subscriptions(mock_get, mock_post, mock_delete, monkeypatch):
    monkeypatch.setenv("TWITCH_WEBHOOK_SECRET", "shh")
    from app.core.config import get_settings

    get_settings.cache_clear()
    mock_post.return_value = _mock_response({"access_token": "tok-1", "expires_in": 3600})
    mock_get.return_value = _mock_response(
        {
            "data": [
                {"id": "sub-1", "condition": {"broadcaster_user_id": "channel-1"}},
                {"id": "sub-2", "condition": {"broadcaster_user_id": "channel-1"}},
                {"id": "sub-3", "condition": {"broadcaster_user_id": "channel-2"}},
            ]
        }
    )
    mock_delete.return_value = _mock_response({}, status_code=204)

    client = TwitchAPIClient(client_id="cid", client_secret="csecret")
    client.unsubscribe_channel("channel-1")

    deleted_ids = {call.kwargs["params"]["id"] for call in mock_delete.call_args_list}
    assert deleted_ids == {"sub-1", "sub-2"}
