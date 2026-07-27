import hashlib
import hmac
import json
from datetime import UTC, datetime
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.webhooks import router
from app.core.db import get_db
from app.models.agreement import Agreement, AgreementStatus, AgreementTermsVersion
from app.models.creator import Creator, CreatorStatus
from app.models.stream_session import StreamSession
from app.services.stream_info import StreamInfo

MESSAGE_ID_HEADER = "Twitch-Eventsub-Message-Id"
TIMESTAMP_HEADER = "Twitch-Eventsub-Message-Timestamp"
SIGNATURE_HEADER = "Twitch-Eventsub-Message-Signature"
TYPE_HEADER = "Twitch-Eventsub-Message-Type"

WEBHOOK_SECRET = "test-webhook-secret"


def _make_app(db_session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    return app


def _sign(body: bytes, message_id="msg-1", timestamp="2026-01-01T00:00:00Z"):
    digest = hmac.new(
        WEBHOOK_SECRET.encode(), (message_id + timestamp).encode() + body, hashlib.sha256
    ).hexdigest()
    return {
        MESSAGE_ID_HEADER: message_id,
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: f"sha256={digest}",
    }


def _authorized_twitch_creator(db_session):
    terms = AgreementTermsVersion(version="v1", effective_date=datetime.now(UTC).date(), body_markdown="x")
    db_session.add(terms)
    db_session.commit()
    creator = Creator(
        platform="twitch", platform_channel_id="channel-1", display_name="A", status=CreatorStatus.AUTHORIZED
    )
    db_session.add(creator)
    db_session.commit()
    db_session.add(
        Agreement(creator_id=creator.id, terms_version_id=terms.id, rev_share_pct=50.0, status=AgreementStatus.ACTIVE)
    )
    db_session.commit()
    return creator


def test_webhook_rejects_invalid_signature(db_session, monkeypatch):
    monkeypatch.setenv("TWITCH_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from app.core.config import get_settings

    get_settings.cache_clear()
    app = _make_app(db_session)
    client = TestClient(app)

    body = b'{"subscription": {"type": "stream.online"}}'
    headers = {
        MESSAGE_ID_HEADER: "msg-1",
        TIMESTAMP_HEADER: "2026-01-01T00:00:00Z",
        SIGNATURE_HEADER: "sha256=deadbeef",
        TYPE_HEADER: "notification",
    }
    response = client.post("/webhooks/twitch/eventsub", content=body, headers=headers)
    assert response.status_code == 403


def test_webhook_rejects_malformed_json_with_valid_signature(db_session, monkeypatch):
    monkeypatch.setenv("TWITCH_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from app.core.config import get_settings

    get_settings.cache_clear()
    app = _make_app(db_session)
    client = TestClient(app)

    body = b"not valid json{"
    headers = _sign(body) | {TYPE_HEADER: "notification"}
    response = client.post("/webhooks/twitch/eventsub", content=body, headers=headers)

    assert response.status_code == 400


def test_webhook_rejects_incomplete_notification_payload_with_valid_signature(db_session, monkeypatch):
    monkeypatch.setenv("TWITCH_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from app.core.config import get_settings

    get_settings.cache_clear()
    app = _make_app(db_session)
    client = TestClient(app)

    # Validly-signed, valid JSON, but missing the expected "subscription"/"event" shape.
    body = json.dumps({"unexpected": "shape"}).encode()
    headers = _sign(body) | {TYPE_HEADER: "notification"}
    response = client.post("/webhooks/twitch/eventsub", content=body, headers=headers)

    assert response.status_code == 400


def test_webhook_handles_verification_challenge(db_session, monkeypatch):
    monkeypatch.setenv("TWITCH_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from app.core.config import get_settings

    get_settings.cache_clear()
    app = _make_app(db_session)
    client = TestClient(app)

    body = json.dumps({"challenge": "abc123"}).encode()
    headers = _sign(body) | {TYPE_HEADER: "webhook_callback_verification"}
    response = client.post("/webhooks/twitch/eventsub", content=body, headers=headers)

    assert response.status_code == 200
    assert response.text == "abc123"


@patch("app.api.webhooks.TwitchAPIClient")
def test_webhook_stream_online_opens_session(mock_client_cls, db_session, monkeypatch):
    monkeypatch.setenv("TWITCH_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from app.core.config import get_settings

    get_settings.cache_clear()
    creator = _authorized_twitch_creator(db_session)
    mock_client_cls.return_value.get_stream_status.return_value = StreamInfo(
        external_stream_id="stream-1", title="t", category="c", viewer_count=10,
        started_at=datetime.now(UTC),
    )
    app = _make_app(db_session)
    client = TestClient(app)

    body = json.dumps(
        {"subscription": {"type": "stream.online"}, "event": {"broadcaster_user_id": "channel-1"}}
    ).encode()
    headers = _sign(body) | {TYPE_HEADER: "notification"}
    response = client.post("/webhooks/twitch/eventsub", content=body, headers=headers)

    assert response.status_code == 200
    session = db_session.query(StreamSession).filter(StreamSession.creator_id == creator.id).first()
    assert session is not None
    assert session.ended_at is None


@patch("app.api.webhooks.TwitchAPIClient")
def test_webhook_stream_offline_closes_session(mock_client_cls, db_session, monkeypatch):
    monkeypatch.setenv("TWITCH_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.services.stream_discovery_service import reconcile_creator_stream_state

    creator = _authorized_twitch_creator(db_session)
    reconcile_creator_stream_state(
        db_session, creator,
        StreamInfo(external_stream_id="stream-1", title="t", category="c", viewer_count=10, started_at=datetime.now(UTC)),
    )
    app = _make_app(db_session)
    client = TestClient(app)

    body = json.dumps(
        {"subscription": {"type": "stream.offline"}, "event": {"broadcaster_user_id": "channel-1"}}
    ).encode()
    headers = _sign(body) | {TYPE_HEADER: "notification"}
    response = client.post("/webhooks/twitch/eventsub", content=body, headers=headers)

    assert response.status_code == 200
    session = db_session.query(StreamSession).filter(StreamSession.creator_id == creator.id).first()
    assert session.ended_at is not None
