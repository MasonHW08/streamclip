# Stream Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Monitor already-authorized creators' livestreams on Twitch (EventSub webhook, backup poll) and YouTube (poll), recording live sessions and viewer-count history — official APIs only, gated by `is_authorized()` on every write.

**Architecture:** New modules in the existing FastAPI monolith. `TwitchClient`/`YouTubeClient` protocols with real + Fake implementations (same pattern as Foundation's `EmailSender`). A single `reconcile_creator_stream_state()` function handles all state transitions, reused by the Twitch webhook handler and both arq cron pollers.

**Tech Stack:** Same as Foundation (Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, arq, httpx). No new dependencies.

## Global Constraints

- Official APIs only — Twitch Helix + EventSub, YouTube Data API v3. No scraping, no unofficial/private endpoints.
- Every code path that reads or writes `stream_sessions`/`viewer_snapshots` for a creator calls `is_authorized(db, creator_id)` first and no-ops if false.
- New settings (`twitch_client_id`, `twitch_client_secret`, `twitch_webhook_secret`, `twitch_eventsub_callback_url`, `youtube_api_key`) default to `""` so dev/tests never need real credentials.
- Real API clients (`TwitchAPIClient`, `YouTubeAPIClient`) are not covered by live-network tests — mirrors Foundation's accepted precedent for `ResendEmailSender`. Pure logic within them (signature verification, token caching, response parsing) IS tested, using monkeypatched `httpx` calls, never real network access.
- `StreamSession` uniqueness is `(platform, external_stream_id)` — `reconcile_creator_stream_state` is idempotent on repeated calls for the same creator/stream.
- Tests run against a real Postgres test database (existing `db_session` fixture from Foundation), never sqlite.
- Follow Foundation's existing module conventions: `app/models/`, `app/services/`, `app/api/`, `app/workers/`.

---

## File Structure

```
backend/
  app/
    models/
      stream_session.py        # StreamSession, ViewerSnapshot
    services/
      stream_info.py            # StreamInfo dataclass (shared by both clients)
      twitch_client.py           # TwitchClient protocol, verify_twitch_signature,
                                  # TwitchAPIClient (real), FakeTwitchClient
      youtube_client.py          # YouTubeClient protocol, YouTubeAPIClient (real),
                                  # FakeYouTubeClient
      stream_discovery_service.py  # list_authorized_creators,
                                     # reconcile_creator_stream_state,
                                     # reconcile_twitch_subscriptions
    api/
      webhooks.py                 # POST /webhooks/twitch/eventsub
    workers/
      stream_discovery_tasks.py   # poll_youtube_streams, poll_twitch_streams_backup,
                                    # reconcile_twitch_subscriptions_task
  migrations/versions/            # new migration for stream_sessions + viewer_snapshots
  tests/
    test_models_stream_session.py
    test_twitch_client.py
    test_youtube_client.py
    test_stream_discovery_service.py
    test_webhooks.py
    test_stream_discovery_tasks.py
```

---

## Task 1: StreamSession + ViewerSnapshot models

**Files:**
- Create: `backend/app/models/stream_session.py`
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/test_models_stream_session.py`

**Interfaces:**
- Consumes: `app.models.creator.Creator` (Foundation)
- Produces: `app.models.stream_session.StreamSession` (`id`, `creator_id: int` FK, `platform: str`, `external_stream_id: str`, `title: str | None`, `category: str | None`, `started_at: datetime`, `ended_at: datetime | None`, `created_at`). Unique constraint on `(platform, external_stream_id)`. Produces `app.models.stream_session.ViewerSnapshot` (`id`, `stream_session_id: int` FK, `viewer_count: int`, `captured_at`).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_models_stream_session.py
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.creator import Creator
from app.models.stream_session import StreamSession, ViewerSnapshot


def _make_creator(db_session):
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    return creator


def test_create_stream_session(db_session):
    creator = _make_creator(db_session)
    session = StreamSession(
        creator_id=creator.id,
        platform="twitch",
        external_stream_id="stream-1",
        title="Ranked grind",
        category="League of Legends",
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    db_session.commit()

    assert session.id is not None
    assert session.ended_at is None


def test_duplicate_platform_external_stream_id_rejected(db_session):
    creator = _make_creator(db_session)
    db_session.add(
        StreamSession(
            creator_id=creator.id, platform="twitch", external_stream_id="1",
            started_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    db_session.add(
        StreamSession(
            creator_id=creator.id, platform="twitch", external_stream_id="1",
            started_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_create_viewer_snapshot(db_session):
    creator = _make_creator(db_session)
    session = StreamSession(
        creator_id=creator.id, platform="twitch", external_stream_id="1",
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    db_session.commit()

    snapshot = ViewerSnapshot(stream_session_id=session.id, viewer_count=42)
    db_session.add(snapshot)
    db_session.commit()

    assert snapshot.id is not None
    assert snapshot.viewer_count == 42
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_models_stream_session.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.stream_session'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/models/stream_session.py
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class StreamSession(Base):
    __tablename__ = "stream_sessions"
    __table_args__ = (UniqueConstraint("platform", "external_stream_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    creator_id: Mapped[int] = mapped_column(ForeignKey("creators.id"))
    platform: Mapped[str] = mapped_column(String(32))
    external_stream_id: Mapped[str] = mapped_column(String(128))
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    category: Mapped[str | None] = mapped_column(String(255), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ViewerSnapshot(Base):
    __tablename__ = "viewer_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    stream_session_id: Mapped[int] = mapped_column(ForeignKey("stream_sessions.id"))
    viewer_count: Mapped[int] = mapped_column(Integer)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

```python
# backend/app/models/__init__.py
from app.models.agreement import Agreement, AgreementTermsVersion
from app.models.base import Base
from app.models.creator import Creator
from app.models.outreach import OutreachEmail
from app.models.stream_session import StreamSession, ViewerSnapshot
from app.models.user import User

__all__ = [
    "Base",
    "Creator",
    "Agreement",
    "AgreementTermsVersion",
    "OutreachEmail",
    "User",
    "StreamSession",
    "ViewerSnapshot",
]
```

- [ ] **Step 4: Run test, generate and apply migration**

Run: `cd backend && pytest tests/test_models_stream_session.py -v` → Expected: PASS
Run: `alembic revision --autogenerate -m "add stream session and viewer snapshot models"`
Run: `DATABASE_URL=postgresql+psycopg://streamclip:streamclip@localhost:5432/streamclip alembic upgrade head`

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/stream_session.py backend/app/models/__init__.py \
        backend/tests/test_models_stream_session.py backend/migrations/versions/
git commit -m "feat: StreamSession and ViewerSnapshot models"
```

---

## Task 2: StreamInfo + TwitchClient protocol + FakeTwitchClient

**Files:**
- Create: `backend/app/services/stream_info.py`
- Create: `backend/app/services/twitch_client.py`
- Test: `backend/tests/test_twitch_client.py`

**Interfaces:**
- Produces: `app.services.stream_info.StreamInfo` (dataclass: `external_stream_id: str`, `title: str | None`, `category: str | None`, `viewer_count: int`, `started_at: datetime`).
- Produces: `app.services.twitch_client.TwitchClient` (Protocol: `get_stream_status(channel_id: str) -> StreamInfo | None`, `list_subscribed_channel_ids() -> set[str]`, `subscribe(channel_id: str, callback_url: str) -> None`, `unsubscribe_channel(channel_id: str) -> None`).
- Produces: `app.services.twitch_client.FakeTwitchClient()` — a controllable test double with `.stream_status: dict[str, StreamInfo]` (test sets this to control what `get_stream_status` returns) and `.subscribed_channel_ids: set[str]` (mutated by `subscribe`/`unsubscribe_channel`).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_twitch_client.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_twitch_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.stream_info'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/services/stream_info.py
from dataclasses import dataclass
from datetime import datetime


@dataclass
class StreamInfo:
    external_stream_id: str
    title: str | None
    category: str | None
    viewer_count: int
    started_at: datetime
```

```python
# backend/app/services/twitch_client.py
from typing import Protocol

from app.services.stream_info import StreamInfo


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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_twitch_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/stream_info.py backend/app/services/twitch_client.py \
        backend/tests/test_twitch_client.py
git commit -m "feat: StreamInfo, TwitchClient protocol, FakeTwitchClient"
```

---

## Task 3: Twitch EventSub signature verification

**Files:**
- Modify: `backend/app/services/twitch_client.py`
- Test: `backend/tests/test_twitch_client.py`

**Interfaces:**
- Produces: `app.services.twitch_client.verify_twitch_signature(secret: str, message_id: str, timestamp: str, body: bytes, signature_header: str) -> bool`. Implements Twitch's documented EventSub webhook signature scheme: HMAC-SHA256 over `message_id + timestamp + body` using `secret`, compared against `signature_header` (format `sha256=<hex>`), using a constant-time comparison.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_twitch_client.py (append)
import hashlib
import hmac

from app.services.twitch_client import verify_twitch_signature


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_twitch_client.py -v -k signature`
Expected: FAIL — `ImportError: cannot import name 'verify_twitch_signature'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/services/twitch_client.py (add near the top, after imports)
import hashlib
import hmac


def verify_twitch_signature(
    secret: str, message_id: str, timestamp: str, body: bytes, signature_header: str
) -> bool:
    if not signature_header.startswith("sha256="):
        return False
    expected_digest = hmac.new(
        secret.encode(), message_id.encode() + timestamp.encode() + body, hashlib.sha256
    ).hexdigest()
    provided_digest = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected_digest, provided_digest)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_twitch_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/twitch_client.py backend/tests/test_twitch_client.py
git commit -m "feat: Twitch EventSub webhook signature verification"
```

---

## Task 4: TwitchAPIClient — app token caching + get_stream_status

**Files:**
- Modify: `backend/app/services/twitch_client.py`
- Test: `backend/tests/test_twitch_client.py`

**Interfaces:**
- Consumes: `app.services.stream_info.StreamInfo` (Task 2)
- Produces: `app.services.twitch_client.TwitchAPIClient(client_id: str, client_secret: str)` implementing `TwitchClient`. `get_stream_status(channel_id)` calls Twitch's Helix "Get Streams" endpoint, using a cached app access token (fetched via OAuth2 client-credentials grant, refreshed on expiry).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_twitch_client.py (append)
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

from app.services.twitch_client import TwitchAPIClient


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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_twitch_client.py -v -k get_stream_status`
Expected: FAIL — `ImportError: cannot import name 'TwitchAPIClient'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/services/twitch_client.py (add after FakeTwitchClient)
from datetime import UTC, datetime, timedelta

import httpx

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_twitch_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/twitch_client.py backend/tests/test_twitch_client.py
git commit -m "feat: TwitchAPIClient app token caching and get_stream_status"
```

---

## Task 5: TwitchAPIClient — EventSub subscription management

**Files:**
- Modify: `backend/app/services/twitch_client.py`
- Test: `backend/tests/test_twitch_client.py`

**Interfaces:**
- Produces: `TwitchAPIClient.list_subscribed_channel_ids() -> set[str]`, `TwitchAPIClient.subscribe(channel_id: str, callback_url: str) -> None` (creates both `stream.online` and `stream.offline` EventSub subscriptions), `TwitchAPIClient.unsubscribe_channel(channel_id: str) -> None` (deletes all subscriptions for that channel). Reads `twitch_webhook_secret` from `get_settings()` for the subscription transport secret.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_twitch_client.py (append)
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_twitch_client.py -v -k "subscribed or subscribe or unsubscribe"`
Expected: FAIL — `AttributeError` (methods not yet implemented on `TwitchAPIClient`)

- [ ] **Step 3: Write the implementation**

```python
# backend/app/services/twitch_client.py (add to TwitchAPIClient, and add import)
from app.core.config import get_settings


class TwitchAPIClient:
    # ... (existing __init__, _get_app_token, _headers, get_stream_status unchanged) ...

    def list_subscribed_channel_ids(self) -> set[str]:
        response = httpx.get(_EVENTSUB_URL, headers=self._headers(), timeout=10.0)
        response.raise_for_status()
        return {
            item["condition"]["broadcaster_user_id"] for item in response.json()["data"]
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
        response = httpx.get(_EVENTSUB_URL, headers=self._headers(), timeout=10.0)
        response.raise_for_status()
        subscription_ids = [
            item["id"]
            for item in response.json()["data"]
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
```

Note: insert `list_subscribed_channel_ids`, `subscribe`, `unsubscribe_channel` as methods on the existing `TwitchAPIClient` class from Task 4 — don't redefine the class, add to it. Add `from app.core.config import get_settings` to the top-level imports.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_twitch_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/twitch_client.py backend/tests/test_twitch_client.py
git commit -m "feat: TwitchAPIClient EventSub subscription management"
```

---

## Task 6: YouTubeClient protocol + FakeYouTubeClient + YouTubeAPIClient

**Files:**
- Create: `backend/app/services/youtube_client.py`
- Test: `backend/tests/test_youtube_client.py`

**Interfaces:**
- Consumes: `app.services.stream_info.StreamInfo` (Task 2)
- Produces: `app.services.youtube_client.YouTubeClient` (Protocol: `get_stream_status(channel_id: str) -> StreamInfo | None`). Produces `app.services.youtube_client.FakeYouTubeClient()` (`.stream_status: dict[str, StreamInfo]`). Produces `app.services.youtube_client.YouTubeAPIClient(api_key: str)` — calls YouTube Data API v3's `search.list(channelId=..., eventType="live", type="video")`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_youtube_client.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_youtube_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.youtube_client'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/services/youtube_client.py
from datetime import datetime
from typing import Protocol

import httpx

from app.services.stream_info import StreamInfo

_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"


class YouTubeClient(Protocol):
    def get_stream_status(self, channel_id: str) -> StreamInfo | None: ...


class FakeYouTubeClient:
    def __init__(self) -> None:
        self.stream_status: dict[str, StreamInfo] = {}

    def get_stream_status(self, channel_id: str) -> StreamInfo | None:
        return self.stream_status.get(channel_id)


class YouTubeAPIClient:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def get_stream_status(self, channel_id: str) -> StreamInfo | None:
        response = httpx.get(
            _SEARCH_URL,
            params={
                "part": "snippet",
                "channelId": channel_id,
                "eventType": "live",
                "type": "video",
                "key": self._api_key,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        items = response.json()["items"]
        if not items:
            return None
        item = items[0]
        # search.list doesn't return viewer count; a follow-up videos.list call
        # (part=liveStreamingDetails) would be needed for that — out of scope
        # for this task, viewer_count defaults to 0 for YouTube for now.
        return StreamInfo(
            external_stream_id=item["id"]["videoId"],
            title=item["snippet"].get("title") or None,
            category=None,
            viewer_count=0,
            started_at=datetime.fromisoformat(
                item["snippet"]["publishTime"].replace("Z", "+00:00")
            ),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_youtube_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/youtube_client.py backend/tests/test_youtube_client.py
git commit -m "feat: YouTubeClient protocol, FakeYouTubeClient, YouTubeAPIClient"
```

---

## Task 7: reconcile_creator_stream_state

**Files:**
- Create: `backend/app/services/stream_discovery_service.py`
- Test: `backend/tests/test_stream_discovery_service.py`

**Interfaces:**
- Consumes: `app.models.stream_session.StreamSession`, `ViewerSnapshot` (Task 1); `app.services.stream_info.StreamInfo` (Task 2); `app.services.permission_gate.is_authorized` (Foundation)
- Produces: `reconcile_creator_stream_state(db: Session, creator: Creator, stream_info: StreamInfo | None) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_stream_discovery_service.py
from datetime import UTC, datetime

from app.models.agreement import Agreement, AgreementStatus, AgreementTermsVersion
from app.models.creator import Creator, CreatorStatus
from app.models.stream_session import StreamSession, ViewerSnapshot
from app.services.stream_discovery_service import reconcile_creator_stream_state
from app.services.stream_info import StreamInfo


def _authorized_creator(db_session):
    terms = AgreementTermsVersion(version="v1", effective_date=datetime.now(UTC).date(), body_markdown="x")
    db_session.add(terms)
    db_session.commit()
    creator = Creator(
        platform="twitch", platform_channel_id="1", display_name="A", status=CreatorStatus.AUTHORIZED
    )
    db_session.add(creator)
    db_session.commit()
    db_session.add(
        Agreement(creator_id=creator.id, terms_version_id=terms.id, rev_share_pct=50.0, status=AgreementStatus.ACTIVE)
    )
    db_session.commit()
    return creator


def _info(stream_id="stream-1", viewer_count=10):
    return StreamInfo(
        external_stream_id=stream_id, title="t", category="c", viewer_count=viewer_count,
        started_at=datetime.now(UTC),
    )


def test_not_live_to_live_opens_session(db_session):
    creator = _authorized_creator(db_session)
    reconcile_creator_stream_state(db_session, creator, _info())

    session = db_session.query(StreamSession).filter(StreamSession.creator_id == creator.id).first()
    assert session is not None
    assert session.ended_at is None
    snapshot = db_session.query(ViewerSnapshot).filter(ViewerSnapshot.stream_session_id == session.id).first()
    assert snapshot.viewer_count == 10


def test_live_to_live_adds_snapshot_not_new_session(db_session):
    creator = _authorized_creator(db_session)
    reconcile_creator_stream_state(db_session, creator, _info(viewer_count=10))
    reconcile_creator_stream_state(db_session, creator, _info(viewer_count=20))

    sessions = db_session.query(StreamSession).filter(StreamSession.creator_id == creator.id).all()
    assert len(sessions) == 1
    snapshots = db_session.query(ViewerSnapshot).filter(ViewerSnapshot.stream_session_id == sessions[0].id).all()
    assert [s.viewer_count for s in snapshots] == [10, 20]


def test_live_to_not_live_closes_session(db_session):
    creator = _authorized_creator(db_session)
    reconcile_creator_stream_state(db_session, creator, _info())
    reconcile_creator_stream_state(db_session, creator, None)

    session = db_session.query(StreamSession).filter(StreamSession.creator_id == creator.id).first()
    assert session.ended_at is not None


def test_unauthorized_creator_is_ignored(db_session):
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()

    reconcile_creator_stream_state(db_session, creator, _info())

    assert db_session.query(StreamSession).filter(StreamSession.creator_id == creator.id).count() == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_stream_discovery_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.stream_discovery_service'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/services/stream_discovery_service.py
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models.creator import Creator
from app.models.stream_session import StreamSession, ViewerSnapshot
from app.services.permission_gate import is_authorized
from app.services.stream_info import StreamInfo


def reconcile_creator_stream_state(
    db: Session, creator: Creator, stream_info: StreamInfo | None
) -> None:
    if not is_authorized(db, creator.id):
        return

    open_session = (
        db.query(StreamSession)
        .filter(StreamSession.creator_id == creator.id, StreamSession.ended_at.is_(None))
        .first()
    )

    if stream_info is None:
        if open_session is not None:
            open_session.ended_at = datetime.now(UTC)
            db.commit()
        return

    if open_session is not None:
        if open_session.external_stream_id == stream_info.external_stream_id:
            db.add(ViewerSnapshot(stream_session_id=open_session.id, viewer_count=stream_info.viewer_count))
            db.commit()
            return
        open_session.ended_at = datetime.now(UTC)

    session = StreamSession(
        creator_id=creator.id,
        platform=creator.platform,
        external_stream_id=stream_info.external_stream_id,
        title=stream_info.title,
        category=stream_info.category,
        started_at=stream_info.started_at,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    db.add(ViewerSnapshot(stream_session_id=session.id, viewer_count=stream_info.viewer_count))
    db.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_stream_discovery_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/stream_discovery_service.py backend/tests/test_stream_discovery_service.py
git commit -m "feat: reconcile_creator_stream_state"
```

---

## Task 8: list_authorized_creators + reconcile_twitch_subscriptions

**Files:**
- Modify: `backend/app/services/stream_discovery_service.py`
- Modify: `backend/tests/test_stream_discovery_service.py`

**Interfaces:**
- Consumes: `app.services.twitch_client.TwitchClient` (Task 2)
- Produces: `list_authorized_creators(db: Session, platform: str) -> list[Creator]`. Produces `reconcile_twitch_subscriptions(db: Session, client: TwitchClient, callback_url: str) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_stream_discovery_service.py (append)
from app.services.stream_discovery_service import list_authorized_creators, reconcile_twitch_subscriptions
from app.services.twitch_client import FakeTwitchClient


def test_list_authorized_creators_filters_by_platform_and_authorization(db_session):
    authorized = _authorized_creator(db_session)
    unauthorized = Creator(platform="twitch", platform_channel_id="2", display_name="B")
    youtube_creator = _authorized_creator(db_session)
    youtube_creator.platform = "youtube"
    youtube_creator.platform_channel_id = "yt-1"
    db_session.add(unauthorized)
    db_session.commit()

    result = list_authorized_creators(db_session, platform="twitch")

    assert [c.id for c in result] == [authorized.id]


def test_reconcile_twitch_subscriptions_subscribes_and_unsubscribes(db_session):
    authorized = _authorized_creator(db_session)
    client = FakeTwitchClient()
    client.subscribed_channel_ids = {authorized.platform_channel_id, "stale-channel"}

    reconcile_twitch_subscriptions(db_session, client, callback_url="https://example.com/webhook")

    # already-authorized creator stays subscribed, untouched
    assert authorized.platform_channel_id in client.subscribed_channel_ids
    # stale subscription (no longer an authorized creator) gets removed
    assert "stale-channel" not in client.subscribed_channel_ids


def test_reconcile_twitch_subscriptions_subscribes_newly_authorized_creator(db_session):
    authorized = _authorized_creator(db_session)
    client = FakeTwitchClient()

    reconcile_twitch_subscriptions(db_session, client, callback_url="https://example.com/webhook")

    assert authorized.platform_channel_id in client.subscribed_channel_ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_stream_discovery_service.py -v -k subscri`
Expected: FAIL — `ImportError: cannot import name 'list_authorized_creators'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/services/stream_discovery_service.py (add)
from app.services.twitch_client import TwitchClient


def list_authorized_creators(db: Session, platform: str) -> list[Creator]:
    creators = db.query(Creator).filter(Creator.platform == platform).all()
    return [c for c in creators if is_authorized(db, c.id)]


def reconcile_twitch_subscriptions(db: Session, client: TwitchClient, callback_url: str) -> None:
    authorized_channel_ids = {
        c.platform_channel_id for c in list_authorized_creators(db, platform="twitch")
    }
    subscribed_channel_ids = client.list_subscribed_channel_ids()

    for channel_id in authorized_channel_ids - subscribed_channel_ids:
        client.subscribe(channel_id, callback_url)
    for channel_id in subscribed_channel_ids - authorized_channel_ids:
        client.unsubscribe_channel(channel_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_stream_discovery_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/stream_discovery_service.py backend/tests/test_stream_discovery_service.py
git commit -m "feat: list_authorized_creators and reconcile_twitch_subscriptions"
```

---

## Task 9: Twitch EventSub webhook route

**Files:**
- Create: `backend/app/api/webhooks.py`
- Test: `backend/tests/test_webhooks.py`

**Interfaces:**
- Consumes: `verify_twitch_signature` (Task 3); `reconcile_creator_stream_state` (Task 7); `app.core.db.get_db` (Foundation)
- Produces: `app.api.webhooks.router` (FastAPI `APIRouter`, prefix `/webhooks/twitch`) with `POST /webhooks/twitch/eventsub`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_webhooks.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_webhooks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.api.webhooks'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/api/webhooks.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_webhooks.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/webhooks.py backend/tests/test_webhooks.py
git commit -m "feat: Twitch EventSub webhook route"
```

---

## Task 10: arq cron tasks

**Files:**
- Create: `backend/app/workers/stream_discovery_tasks.py`
- Modify: `backend/app/workers/settings.py`
- Test: `backend/tests/test_stream_discovery_tasks.py`

**Interfaces:**
- Consumes: `list_authorized_creators`, `reconcile_creator_stream_state`, `reconcile_twitch_subscriptions` (Tasks 7-8); `TwitchAPIClient`, `YouTubeAPIClient` (Tasks 4-6)
- Produces: `poll_youtube_streams(ctx) -> None`, `poll_twitch_streams_backup(ctx) -> None`, `reconcile_twitch_subscriptions_task(ctx) -> None`. Modifies `app.workers.settings.WorkerSettings` to register these as `cron_jobs`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_stream_discovery_tasks.py
from datetime import UTC, datetime

import pytest

from app.models.agreement import Agreement, AgreementStatus, AgreementTermsVersion
from app.models.creator import Creator, CreatorStatus
from app.models.stream_session import StreamSession
from app.services.stream_info import StreamInfo
from app.services.twitch_client import FakeTwitchClient
from app.services.youtube_client import FakeYouTubeClient
from app.workers.stream_discovery_tasks import poll_twitch_streams_backup, poll_youtube_streams


def _authorized_creator(db_session, platform, channel_id):
    terms = AgreementTermsVersion(version="v1", effective_date=datetime.now(UTC).date(), body_markdown="x")
    db_session.add(terms)
    db_session.commit()
    creator = Creator(
        platform=platform, platform_channel_id=channel_id, display_name="A", status=CreatorStatus.AUTHORIZED
    )
    db_session.add(creator)
    db_session.commit()
    db_session.add(
        Agreement(creator_id=creator.id, terms_version_id=terms.id, rev_share_pct=50.0, status=AgreementStatus.ACTIVE)
    )
    db_session.commit()
    return creator


@pytest.mark.asyncio
async def test_poll_youtube_streams_opens_session_for_live_creator(db_session, monkeypatch):
    monkeypatch.setattr("app.workers.stream_discovery_tasks.SessionLocal", lambda: db_session)
    creator = _authorized_creator(db_session, "youtube", "yt-1")

    fake_client = FakeYouTubeClient()
    fake_client.stream_status["yt-1"] = StreamInfo(
        external_stream_id="vid-1", title="t", category=None, viewer_count=5, started_at=datetime.now(UTC)
    )
    monkeypatch.setattr(
        "app.workers.stream_discovery_tasks.YouTubeAPIClient", lambda api_key: fake_client
    )

    await poll_youtube_streams({})

    session = db_session.query(StreamSession).filter(StreamSession.creator_id == creator.id).first()
    assert session is not None


@pytest.mark.asyncio
async def test_poll_twitch_streams_backup_closes_stale_session(db_session, monkeypatch):
    monkeypatch.setattr("app.workers.stream_discovery_tasks.SessionLocal", lambda: db_session)
    from app.services.stream_discovery_service import reconcile_creator_stream_state

    creator = _authorized_creator(db_session, "twitch", "channel-1")
    reconcile_creator_stream_state(
        db_session, creator,
        StreamInfo(external_stream_id="s1", title="t", category="c", viewer_count=1, started_at=datetime.now(UTC)),
    )

    fake_client = FakeTwitchClient()  # no stream_status configured -> not live
    monkeypatch.setattr(
        "app.workers.stream_discovery_tasks.TwitchAPIClient",
        lambda client_id, client_secret: fake_client,
    )

    await poll_twitch_streams_backup({})

    session = db_session.query(StreamSession).filter(StreamSession.creator_id == creator.id).first()
    assert session.ended_at is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_stream_discovery_tasks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.workers.stream_discovery_tasks'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/workers/stream_discovery_tasks.py
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.stream_discovery_service import (
    list_authorized_creators,
    reconcile_creator_stream_state,
    reconcile_twitch_subscriptions,
)
from app.services.twitch_client import TwitchAPIClient
from app.services.youtube_client import YouTubeAPIClient


async def poll_youtube_streams(ctx: dict) -> None:
    settings = get_settings()
    client = YouTubeAPIClient(api_key=settings.youtube_api_key)
    db = SessionLocal()
    try:
        for creator in list_authorized_creators(db, platform="youtube"):
            stream_info = client.get_stream_status(creator.platform_channel_id)
            reconcile_creator_stream_state(db, creator, stream_info)
    finally:
        db.close()


async def poll_twitch_streams_backup(ctx: dict) -> None:
    settings = get_settings()
    client = TwitchAPIClient(
        client_id=settings.twitch_client_id, client_secret=settings.twitch_client_secret
    )
    db = SessionLocal()
    try:
        for creator in list_authorized_creators(db, platform="twitch"):
            stream_info = client.get_stream_status(creator.platform_channel_id)
            reconcile_creator_stream_state(db, creator, stream_info)
    finally:
        db.close()


async def reconcile_twitch_subscriptions_task(ctx: dict) -> None:
    settings = get_settings()
    client = TwitchAPIClient(
        client_id=settings.twitch_client_id, client_secret=settings.twitch_client_secret
    )
    db = SessionLocal()
    try:
        reconcile_twitch_subscriptions(db, client, callback_url=settings.twitch_eventsub_callback_url)
    finally:
        db.close()
```

```python
# backend/app/workers/settings.py (replace entire file)
from arq.connections import RedisSettings
from arq.cron import cron

from app.core.config import get_settings
from app.workers.stream_discovery_tasks import (
    poll_twitch_streams_backup,
    poll_youtube_streams,
    reconcile_twitch_subscriptions_task,
)
from app.workers.tasks import send_approved_outreach_email


class WorkerSettings:
    functions = [send_approved_outreach_email]
    cron_jobs = [
        cron(poll_youtube_streams, minute=set(range(0, 60, 5))),
        cron(poll_twitch_streams_backup, minute=set(range(0, 60, 15))),
        cron(reconcile_twitch_subscriptions_task, minute=set(range(0, 60, 30))),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_stream_discovery_tasks.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/workers/stream_discovery_tasks.py backend/app/workers/settings.py \
        backend/tests/test_stream_discovery_tasks.py
git commit -m "feat: arq cron jobs for stream polling and subscription reconciliation"
```

---

## Task 11: Settings, wiring, and docs

**Files:**
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/main.py`
- Modify: `.env.example`
- Modify: `README.md`
- Test: `backend/tests/test_main.py`

**Interfaces:**
- Modifies `app.core.config.Settings` to add: `twitch_client_id: str = ""`, `twitch_client_secret: str = ""`, `twitch_webhook_secret: str = ""`, `twitch_eventsub_callback_url: str = ""`, `youtube_api_key: str = ""`.
- Modifies `app.main.app` to include `app.api.webhooks.router`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_main.py (append)
def test_webhooks_router_is_registered():
    from app.main import app

    paths = {route.path for route in app.routes}
    assert "/webhooks/twitch/eventsub" in paths
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_main.py -v -k webhooks`
Expected: FAIL — `AssertionError` (route not registered yet)

- [ ] **Step 3: Write the implementation**

```python
# backend/app/core/config.py — add these fields to the Settings class
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str = "redis://localhost:6379/0"
    resend_api_key: str = ""
    resend_from_address: str = "partners@streamclip.co"
    jwt_secret: str
    magic_link_expiry_days: int = 14
    public_base_url: str = "http://localhost:8000"
    default_rev_share_pct: float = 50.0
    environment: str = "development"
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    twitch_webhook_secret: str = ""
    twitch_eventsub_callback_url: str = ""
    youtube_api_key: str = ""
```

(Add the five new fields; leave every existing field and the JWT_SECRET validator from Foundation unchanged.)

```python
# backend/app/main.py — add the import and include_router call
from app.api.webhooks import router as webhooks_router

# ... after app.include_router(internal_router):
app.include_router(webhooks_router)
```

```bash
# .env.example — append
TWITCH_CLIENT_ID=
TWITCH_CLIENT_SECRET=
TWITCH_WEBHOOK_SECRET=
TWITCH_EVENTSUB_CALLBACK_URL=https://your-deployed-domain.example/webhooks/twitch/eventsub
YOUTUBE_API_KEY=
```

README.md — append a new section:

```markdown
## Stream Discovery (sub-project 2)

Monitors already-authorized creators' livestreams on Twitch (via EventSub
webhook, `POST /webhooks/twitch/eventsub`) and YouTube (via polling). Nothing
here works without real credentials:

- **Twitch**: register an app at https://dev.twitch.tv/console, set
  `TWITCH_CLIENT_ID`/`TWITCH_CLIENT_SECRET`. Set `TWITCH_WEBHOOK_SECRET` to any
  long random string (used to verify EventSub payloads). Set
  `TWITCH_EVENTSUB_CALLBACK_URL` to your deployed app's public URL + the
  webhook path — Twitch must be able to reach it, so this doesn't work against
  `localhost` without a tunnel (e.g. ngrok) during development.
- **YouTube**: create a Google Cloud project, enable the YouTube Data API v3,
  create an API key, set `YOUTUBE_API_KEY`. Be mindful of the default 10,000
  units/day quota — `search.list` (used to check live status) costs 100 units
  per call, so polling many creators frequently adds up fast; the poll
  interval is set in `app/workers/settings.py`'s `cron_jobs`.

Until these are set, the system runs fine with `Fake*Client` test doubles in
tests, but the real workers (`poll_youtube_streams`,
`poll_twitch_streams_backup`, `reconcile_twitch_subscriptions_task`) will
either no-op or error against a live Twitch/YouTube API without them.
```

- [ ] **Step 4: Run test, then full suite**

Run: `cd backend && pytest tests/test_main.py -v -k webhooks` → Expected: PASS
Run: `cd backend && pytest -v` → Expected: all PASS (Foundation's 88 tests + this sub-project's new tests)
Run: `cd backend && ruff check . && mypy app` → Expected: both clean

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/config.py backend/app/main.py .env.example README.md \
        backend/tests/test_main.py
git commit -m "feat: wire up webhooks router, add Twitch/YouTube settings, docs"
```

---

## Self-Review Notes

- **Spec coverage:** every piece from the design spec maps to a task — data model (Task 1), platform clients + signature verification + token caching + subscription management (Tasks 2-6), the core reconciliation function and authorized-creator listing (Tasks 7-8), the webhook receiver (Task 9), the three cron jobs (Task 10), and settings/wiring/docs (Task 11).
- **`is_authorized()` gate discipline:** verified every write path — `reconcile_creator_stream_state` (used by the webhook and both pollers) checks it first; `list_authorized_creators`/`reconcile_twitch_subscriptions` filter by it directly. No path bypasses the check.
- **Out-of-scope items confirmed absent:** no creator-prospecting/discovery logic, no Kick support, no dashboard/UI — matches the spec's explicit exclusions.
- **Type/signature consistency checked:** `StreamInfo`, `TwitchClient`/`YouTubeClient` protocol methods, `reconcile_creator_stream_state(db, creator, stream_info)`, `list_authorized_creators(db, platform)`, and `reconcile_twitch_subscriptions(db, client, callback_url)` are each defined once and used with matching signatures everywhere they're consumed.
- **Real-vs-fake test coverage boundary is deliberate, not a gap:** `TwitchAPIClient`/`YouTubeAPIClient`'s HTTP-calling logic (token caching, response parsing, subscription CRUD) is tested via monkeypatched `httpx`, never real network access; `verify_twitch_signature` (pure function) has full positive/negative test coverage. This mirrors Foundation's accepted precedent for `ResendEmailSender`.
