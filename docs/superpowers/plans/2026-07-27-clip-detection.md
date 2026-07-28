# Clip Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect clip-worthy moments in authorized creators' livestreams by ingesting chat activity (Twitch via a real-time EventSub WebSocket, YouTube via polling) and scoring message-rate spikes into stored `ClipCandidate` records.

**Architecture:** New modules in the existing FastAPI + arq monolith. A one-time admin OAuth flow links a dedicated Twitch bot account (needed because reading chat requires a user token, not the app-only token Foundation/Stream Discovery already use). A long-running arq job maintains the Twitch WebSocket per live creator; a cron job polls YouTube; a third cron job runs the detection algorithm over accumulated buckets.

**Tech Stack:** Same as Foundation/Stream Discovery (Python 3.12, FastAPI, SQLAlchemy 2.0, Alembic, arq, httpx), plus `websockets` (new dependency, for the Twitch EventSub WebSocket transport).

## Global Constraints

- Every code path that reads or writes chat/clip data for a creator calls `is_authorized(db, creator_id)` first and no-ops if false.
- The Twitch chat listener is long-lived (can run for hours) — it must NOT check authorization once at start and trust it. It re-checks fresh from the DB (never a cached object) on every bucket flush, and disconnects immediately if a creator revokes mid-stream. This directly carries forward Stream Discovery's Critical finding (identity-map staleness in long-running/looping code).
- API keys/tokens are NEVER placed in URL query parameters — always in headers (`X-goog-api-key` for YouTube, `Authorization: Bearer` for Twitch). This directly carries forward Stream Discovery's other Critical finding (a key in a query param leaks into `HTTPStatusError` messages and logs).
- OAuth `state` parameters are signed JWTs (reusing `settings.jwt_secret`, already required to be a strong secret by Foundation's validator) with a 10-minute expiry — no server-side session storage needed.
- v1 scoring is chat-message-rate spikes only (mean + k·stddev over a rolling window) — no LLM/AI scoring, no viewer-count signal (granularity mismatch, deferred).
- Real API/WebSocket clients (`TwitchEventSubChatClient`, `YouTubeChatAPIClient`) are not covered by live-network tests — mirrors the established precedent from Foundation/Stream Discovery. Pure logic (bucket flush, detection algorithm, OAuth token exchange/refresh, WebSocket message parsing) IS tested, using mocked httpx/websockets, never real network access.
- Tests run against a real Postgres test database (existing `db_session` fixture), never sqlite.
- New settings default to safe values (`""` for secrets/URIs, conservative numeric defaults) so dev/tests never need real credentials or a linked bot account.

---

## File Structure

```
backend/
  app/
    models/
      chat_activity.py           # ChatActivityBucket, ClipCandidate, YouTubeChatPollState
      twitch_bot_credential.py   # TwitchBotCredential
    services/
      twitch_bot_service.py       # get_valid_bot_access_token, get_bot_user_id
      twitch_chat_client.py       # TwitchChatClient protocol, Fake*, TwitchEventSubChatClient (real)
      youtube_chat_client.py      # YouTubeChatClient protocol, Fake*, YouTubeChatAPIClient (real)
      clip_detection_service.py   # record_chat_activity, detect_clip_candidates_for_session
    api/
      twitch_bot_oauth.py          # GET /internal/twitch-bot/authorize, /callback
    workers/
      chat_tasks.py                # listen_to_twitch_chat, poll_youtube_chat, detect_clip_candidates
  migrations/versions/             # two new migrations
  tests/
    test_models_chat_activity.py
    test_twitch_bot_service.py
    test_twitch_bot_oauth.py
    test_twitch_chat_client.py
    test_youtube_chat_client.py
    test_clip_detection_service.py
    test_chat_tasks.py
```

---

## Task 1: ChatActivityBucket, ClipCandidate, YouTubeChatPollState models

**Files:**
- Create: `backend/app/models/chat_activity.py`
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/test_models_chat_activity.py`

**Interfaces:**
- Consumes: `app.models.stream_session.StreamSession` (Stream Discovery)
- Produces: `ChatActivityBucket` (`id`, `stream_session_id: int` FK, `bucket_start: datetime`, `message_count: int`). Unique `(stream_session_id, bucket_start)`. Produces `ClipCandidate` (`id`, `stream_session_id: int` FK, `start_at: datetime`, `end_at: datetime`, `score: float`, `signal_type: str`, `created_at: datetime`). Produces `YouTubeChatPollState` (`stream_session_id: int` FK, primary key; `next_page_token: str | None`).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_models_chat_activity.py
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.chat_activity import ChatActivityBucket, ClipCandidate, YouTubeChatPollState
from app.models.creator import Creator
from app.models.stream_session import StreamSession


def _make_session(db_session):
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    session = StreamSession(
        creator_id=creator.id, platform="twitch", external_stream_id="s1",
        started_at=datetime.now(UTC),
    )
    db_session.add(session)
    db_session.commit()
    return session


def test_create_chat_activity_bucket(db_session):
    session = _make_session(db_session)
    bucket = ChatActivityBucket(
        stream_session_id=session.id, bucket_start=datetime.now(UTC), message_count=5,
    )
    db_session.add(bucket)
    db_session.commit()
    assert bucket.id is not None


def test_duplicate_bucket_rejected(db_session):
    session = _make_session(db_session)
    bucket_start = datetime.now(UTC)
    db_session.add(ChatActivityBucket(stream_session_id=session.id, bucket_start=bucket_start, message_count=1))
    db_session.commit()
    db_session.add(ChatActivityBucket(stream_session_id=session.id, bucket_start=bucket_start, message_count=2))
    with pytest.raises(IntegrityError):
        db_session.commit()


def test_create_clip_candidate(db_session):
    session = _make_session(db_session)
    now = datetime.now(UTC)
    candidate = ClipCandidate(
        stream_session_id=session.id, start_at=now, end_at=now, score=3.2, signal_type="chat_spike",
    )
    db_session.add(candidate)
    db_session.commit()
    assert candidate.id is not None
    assert candidate.signal_type == "chat_spike"


def test_create_youtube_chat_poll_state(db_session):
    session = _make_session(db_session)
    state = YouTubeChatPollState(stream_session_id=session.id, next_page_token="abc")
    db_session.add(state)
    db_session.commit()
    assert state.stream_session_id == session.id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_models_chat_activity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.chat_activity'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/models/chat_activity.py
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ChatActivityBucket(Base):
    __tablename__ = "chat_activity_buckets"
    __table_args__ = (UniqueConstraint("stream_session_id", "bucket_start"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stream_session_id: Mapped[int] = mapped_column(ForeignKey("stream_sessions.id"))
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    message_count: Mapped[int] = mapped_column(Integer, default=0)


class ClipCandidate(Base):
    __tablename__ = "clip_candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    stream_session_id: Mapped[int] = mapped_column(ForeignKey("stream_sessions.id"))
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    score: Mapped[float] = mapped_column(Float)
    signal_type: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class YouTubeChatPollState(Base):
    __tablename__ = "youtube_chat_poll_states"

    stream_session_id: Mapped[int] = mapped_column(ForeignKey("stream_sessions.id"), primary_key=True)
    next_page_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
```

```python
# backend/app/models/__init__.py
from app.models.agreement import Agreement, AgreementTermsVersion
from app.models.base import Base
from app.models.chat_activity import ChatActivityBucket, ClipCandidate, YouTubeChatPollState
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
    "ChatActivityBucket",
    "ClipCandidate",
    "YouTubeChatPollState",
]
```

- [ ] **Step 4: Run test, generate and apply migration**

Run: `cd backend && pytest tests/test_models_chat_activity.py -v` → Expected: PASS
Run: `alembic revision --autogenerate -m "add chat activity, clip candidate, youtube poll state models"`
Run: `DATABASE_URL=postgresql+psycopg://streamclip:streamclip@localhost:5432/streamclip alembic upgrade head`

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/chat_activity.py backend/app/models/__init__.py \
        backend/tests/test_models_chat_activity.py backend/migrations/versions/
git commit -m "feat: ChatActivityBucket, ClipCandidate, YouTubeChatPollState models"
```

---

## Task 2: TwitchBotCredential model

**Files:**
- Create: `backend/app/models/twitch_bot_credential.py`
- Modify: `backend/app/models/__init__.py`
- Test: `backend/tests/test_models_twitch_bot_credential.py`

**Interfaces:**
- Produces: `TwitchBotCredential` (`id`, `bot_user_id: str`, `access_token: str`, `refresh_token: str`, `expires_at: datetime`, `updated_at: datetime`). Single-row table in practice — no uniqueness constraint needed since the app always queries "the most recent row" via `.first()` ordered by nothing special (only one row is ever expected to exist; the OAuth callback upserts it).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_models_twitch_bot_credential.py
from datetime import UTC, datetime, timedelta

from app.models.twitch_bot_credential import TwitchBotCredential


def test_create_twitch_bot_credential(db_session):
    credential = TwitchBotCredential(
        bot_user_id="12345",
        access_token="access-abc",
        refresh_token="refresh-xyz",
        expires_at=datetime.now(UTC) + timedelta(hours=4),
    )
    db_session.add(credential)
    db_session.commit()

    assert credential.id is not None
    assert credential.bot_user_id == "12345"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_models_twitch_bot_credential.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.models.twitch_bot_credential'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/models/twitch_bot_credential.py
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TwitchBotCredential(Base):
    __tablename__ = "twitch_bot_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    bot_user_id: Mapped[str] = mapped_column(String(64))
    access_token: Mapped[str] = mapped_column(String(255))
    refresh_token: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
```

```python
# backend/app/models/__init__.py (add to existing file)
from app.models.twitch_bot_credential import TwitchBotCredential

# add "TwitchBotCredential" to __all__
```

- [ ] **Step 4: Run test, generate and apply migration**

Run: `cd backend && pytest tests/test_models_twitch_bot_credential.py -v` → Expected: PASS
Run: `alembic revision --autogenerate -m "add twitch bot credential model"`
Run: `DATABASE_URL=postgresql+psycopg://streamclip:streamclip@localhost:5432/streamclip alembic upgrade head`

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/twitch_bot_credential.py backend/app/models/__init__.py \
        backend/tests/test_models_twitch_bot_credential.py backend/migrations/versions/
git commit -m "feat: TwitchBotCredential model"
```

---

## Task 3: Twitch bot OAuth authorize/callback routes

**Files:**
- Create: `backend/app/api/twitch_bot_oauth.py`
- Test: `backend/tests/test_twitch_bot_oauth.py`

**Interfaces:**
- Consumes: `app.api.internal_auth.require_internal_user` (Foundation); `app.models.twitch_bot_credential.TwitchBotCredential` (Task 2)
- Produces: `app.api.twitch_bot_oauth.router` (prefix `/internal/twitch-bot`) with `GET /authorize` (auth-gated, redirects to Twitch) and `GET /callback` (no internal-auth gate — protected by `state` verification instead, since Twitch's redirect can't carry our Basic-auth headers). Produces `create_oauth_state()` / `verify_oauth_state(state) -> bool` helpers.
- Adds `twitch_bot_oauth_redirect_uri: str = ""` to `Settings`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_twitch_bot_oauth.py
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.twitch_bot_oauth import create_oauth_state, router, verify_oauth_state
from app.core.db import get_db
from app.models.twitch_bot_credential import TwitchBotCredential
from app.models.user import User
from app.services.password import hash_password


def _make_app(db_session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    return app


def test_oauth_state_roundtrip():
    state = create_oauth_state()
    assert verify_oauth_state(state) is True
    assert verify_oauth_state("garbage") is False


def test_authorize_requires_internal_auth(db_session):
    app = _make_app(db_session)
    client = TestClient(app)
    response = client.get("/internal/twitch-bot/authorize", follow_redirects=False)
    assert response.status_code == 401


def test_authorize_redirects_to_twitch_with_state(db_session, monkeypatch):
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_BOT_OAUTH_REDIRECT_URI", "https://example.com/internal/twitch-bot/callback")
    from app.core.config import get_settings

    get_settings.cache_clear()
    user = User(email="team@streamclip.co", hashed_password=hash_password("secret"), role="admin")
    db_session.add(user)
    db_session.commit()

    app = _make_app(db_session)
    client = TestClient(app)
    response = client.get(
        "/internal/twitch-bot/authorize", auth=("team@streamclip.co", "secret"), follow_redirects=False
    )
    assert response.status_code in (302, 307)
    assert "id.twitch.tv/oauth2/authorize" in response.headers["location"]
    assert "client_id=cid" in response.headers["location"]
    assert "state=" in response.headers["location"]


def test_callback_rejects_invalid_state(db_session):
    app = _make_app(db_session)
    client = TestClient(app)
    response = client.get("/internal/twitch-bot/callback", params={"code": "abc", "state": "garbage"})
    assert response.status_code == 400


@patch("app.api.twitch_bot_oauth.httpx.get")
@patch("app.api.twitch_bot_oauth.httpx.post")
def test_callback_exchanges_code_and_stores_credential(mock_post, mock_get, db_session, monkeypatch):
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("TWITCH_BOT_OAUTH_REDIRECT_URI", "https://example.com/internal/twitch-bot/callback")
    from app.core.config import get_settings

    get_settings.cache_clear()

    token_response = MagicMock()
    token_response.raise_for_status = MagicMock()
    token_response.json.return_value = {
        "access_token": "access-abc", "refresh_token": "refresh-xyz", "expires_in": 14400,
    }
    mock_post.return_value = token_response

    users_response = MagicMock()
    users_response.raise_for_status = MagicMock()
    users_response.json.return_value = {"data": [{"id": "999"}]}
    mock_get.return_value = users_response

    app = _make_app(db_session)
    client = TestClient(app)
    state = create_oauth_state()
    response = client.get("/internal/twitch-bot/callback", params={"code": "auth-code", "state": state})

    assert response.status_code == 200
    credential = db_session.query(TwitchBotCredential).first()
    assert credential is not None
    assert credential.bot_user_id == "999"
    assert credential.access_token == "access-abc"
```

Note: `create_oauth_state`/`verify_oauth_state` don't depend on env vars, so `test_oauth_state_roundtrip` needs `JWT_SECRET` set — it already is, via `conftest.py`'s session-wide `os.environ.setdefault`.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_twitch_bot_oauth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.api.twitch_bot_oauth'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/core/config.py — add this field to the Settings class
    twitch_bot_oauth_redirect_uri: str = ""
```

```python
# backend/app/api/twitch_bot_oauth.py
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.internal_auth import require_internal_user
from app.core.config import get_settings
from app.core.db import get_db
from app.models.twitch_bot_credential import TwitchBotCredential
from app.models.user import User

router = APIRouter(prefix="/internal/twitch-bot", tags=["internal"])

_AUTHORIZE_URL = "https://id.twitch.tv/oauth2/authorize"
_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_USERS_URL = "https://api.twitch.tv/helix/users"
_OAUTH_STATE_PURPOSE = "twitch_bot_oauth"


def create_oauth_state() -> str:
    settings = get_settings()
    payload = {
        "purpose": _OAUTH_STATE_PURPOSE,
        "exp": datetime.now(UTC) + timedelta(minutes=10),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def verify_oauth_state(state: str) -> bool:
    settings = get_settings()
    try:
        payload = jwt.decode(state, settings.jwt_secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return False
    return bool(payload.get("purpose") == _OAUTH_STATE_PURPOSE)


@router.get("/authorize")
def start_authorize(_user: User = Depends(require_internal_user)) -> RedirectResponse:
    settings = get_settings()
    params = {
        "client_id": settings.twitch_client_id,
        "redirect_uri": settings.twitch_bot_oauth_redirect_uri,
        "response_type": "code",
        "scope": "user:read:chat",
        "state": create_oauth_state(),
    }
    return RedirectResponse(f"{_AUTHORIZE_URL}?{urlencode(params)}")


@router.get("/callback")
def oauth_callback(code: str, state: str, db: Session = Depends(get_db)) -> dict:
    if not verify_oauth_state(state):
        raise HTTPException(status_code=400, detail="Invalid or expired state")

    settings = get_settings()
    token_response = httpx.post(
        _TOKEN_URL,
        data={
            "client_id": settings.twitch_client_id,
            "client_secret": settings.twitch_client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": settings.twitch_bot_oauth_redirect_uri,
        },
        timeout=10.0,
    )
    token_response.raise_for_status()
    token_payload = token_response.json()

    users_response = httpx.get(
        _USERS_URL,
        headers={
            "Client-Id": settings.twitch_client_id,
            "Authorization": f"Bearer {token_payload['access_token']}",
        },
        timeout=10.0,
    )
    users_response.raise_for_status()
    bot_user_id = users_response.json()["data"][0]["id"]

    expires_at = datetime.now(UTC) + timedelta(seconds=token_payload["expires_in"])
    credential = db.query(TwitchBotCredential).first()
    if credential is None:
        credential = TwitchBotCredential(
            bot_user_id=bot_user_id,
            access_token=token_payload["access_token"],
            refresh_token=token_payload["refresh_token"],
            expires_at=expires_at,
        )
        db.add(credential)
    else:
        credential.bot_user_id = bot_user_id
        credential.access_token = token_payload["access_token"]
        credential.refresh_token = token_payload["refresh_token"]
        credential.expires_at = expires_at
    db.commit()

    return {"status": "linked", "bot_user_id": bot_user_id}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_twitch_bot_oauth.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/twitch_bot_oauth.py backend/app/core/config.py \
        backend/tests/test_twitch_bot_oauth.py
git commit -m "feat: Twitch bot OAuth authorize/callback routes"
```

---

## Task 4: Bot token refresh helper

**Files:**
- Create: `backend/app/services/twitch_bot_service.py`
- Test: `backend/tests/test_twitch_bot_service.py`

**Interfaces:**
- Consumes: `app.models.twitch_bot_credential.TwitchBotCredential` (Task 2)
- Produces: `get_valid_bot_access_token(db: Session) -> str` (returns the cached token if not near expiry, otherwise refreshes and persists the new one first). Produces `get_bot_user_id(db: Session) -> str`. Both raise `ValueError` if no bot account has been linked yet.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_twitch_bot_service.py
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app.models.twitch_bot_credential import TwitchBotCredential
from app.services.twitch_bot_service import get_bot_user_id, get_valid_bot_access_token


def test_raises_if_no_credential_linked(db_session):
    with pytest.raises(ValueError, match="No Twitch bot account linked"):
        get_valid_bot_access_token(db_session)


def test_returns_cached_token_when_not_near_expiry(db_session):
    db_session.add(
        TwitchBotCredential(
            bot_user_id="1", access_token="fresh-token", refresh_token="r",
            expires_at=datetime.now(UTC) + timedelta(hours=2),
        )
    )
    db_session.commit()

    assert get_valid_bot_access_token(db_session) == "fresh-token"


@patch("app.services.twitch_bot_service.httpx.post")
def test_refreshes_token_when_near_expiry(mock_post, db_session):
    credential = TwitchBotCredential(
        bot_user_id="1", access_token="stale-token", refresh_token="old-refresh",
        expires_at=datetime.now(UTC) + timedelta(seconds=30),
    )
    db_session.add(credential)
    db_session.commit()

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "access_token": "new-token", "refresh_token": "new-refresh", "expires_in": 14400,
    }
    mock_post.return_value = response

    token = get_valid_bot_access_token(db_session)

    assert token == "new-token"
    db_session.refresh(credential)
    assert credential.access_token == "new-token"
    assert credential.refresh_token == "new-refresh"


def test_get_bot_user_id_returns_stored_id(db_session):
    db_session.add(
        TwitchBotCredential(
            bot_user_id="999", access_token="t", refresh_token="r",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    db_session.commit()

    assert get_bot_user_id(db_session) == "999"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_twitch_bot_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.twitch_bot_service'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/services/twitch_bot_service.py
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.twitch_bot_credential import TwitchBotCredential

_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_REFRESH_BUFFER_SECONDS = 60


def _get_credential(db: Session) -> TwitchBotCredential:
    credential = db.query(TwitchBotCredential).first()
    if credential is None:
        raise ValueError(
            "No Twitch bot account linked yet — visit /internal/twitch-bot/authorize"
        )
    return credential


def get_valid_bot_access_token(db: Session) -> str:
    credential = _get_credential(db)
    if datetime.now(UTC) < credential.expires_at - timedelta(seconds=_REFRESH_BUFFER_SECONDS):
        return credential.access_token

    settings = get_settings()
    response = httpx.post(
        _TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": credential.refresh_token,
            "client_id": settings.twitch_client_id,
            "client_secret": settings.twitch_client_secret,
        },
        timeout=10.0,
    )
    response.raise_for_status()
    payload = response.json()

    credential.access_token = payload["access_token"]
    credential.refresh_token = payload.get("refresh_token", credential.refresh_token)
    credential.expires_at = datetime.now(UTC) + timedelta(seconds=payload["expires_in"])
    db.commit()
    return credential.access_token


def get_bot_user_id(db: Session) -> str:
    return _get_credential(db).bot_user_id
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_twitch_bot_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/twitch_bot_service.py backend/tests/test_twitch_bot_service.py
git commit -m "feat: Twitch bot access token refresh helper"
```

---

## Task 5: TwitchChatClient protocol + FakeTwitchChatClient

**Files:**
- Create: `backend/app/services/twitch_chat_client.py`
- Test: `backend/tests/test_twitch_chat_client.py`

**Interfaces:**
- Produces: `app.services.twitch_chat_client.TwitchChatConnection` (Protocol: `async def receive_message(self) -> datetime | None` — returns a timestamp per chat message received, or `None` when the connection closes; `async def close(self) -> None`). Produces `app.services.twitch_chat_client.TwitchChatClient` (Protocol: `async def connect(self, channel_id: str, bot_access_token: str, bot_user_id: str) -> TwitchChatConnection`). Produces `FakeTwitchChatConnection` (test double: `.push(timestamp)`, `.end()`, `.closed: bool`) and `FakeTwitchChatClient` (`.connections: dict[str, FakeTwitchChatConnection]`).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_twitch_chat_client.py
from datetime import UTC, datetime

import pytest

from app.services.twitch_chat_client import FakeTwitchChatClient


@pytest.mark.asyncio
async def test_fake_client_connection_yields_pushed_messages():
    client = FakeTwitchChatClient()
    connection = await client.connect("channel-1", "token", "bot-id")

    await connection.push(datetime(2026, 1, 1, tzinfo=UTC))
    await connection.push(datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC))
    await connection.end()

    first = await connection.receive_message()
    second = await connection.receive_message()
    third = await connection.receive_message()

    assert first == datetime(2026, 1, 1, tzinfo=UTC)
    assert second == datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    assert third is None


@pytest.mark.asyncio
async def test_fake_client_close_marks_connection_closed():
    client = FakeTwitchChatClient()
    connection = await client.connect("channel-1", "token", "bot-id")
    await connection.close()
    assert connection.closed is True


@pytest.mark.asyncio
async def test_fake_client_reuses_same_connection_for_same_channel():
    client = FakeTwitchChatClient()
    connection_a = await client.connect("channel-1", "token", "bot-id")
    connection_b = await client.connect("channel-1", "token", "bot-id")
    assert connection_a is connection_b
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_twitch_chat_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.twitch_chat_client'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/services/twitch_chat_client.py
import asyncio
from datetime import datetime
from typing import Protocol


class TwitchChatConnection(Protocol):
    async def receive_message(self) -> datetime | None: ...
    async def close(self) -> None: ...


class TwitchChatClient(Protocol):
    async def connect(
        self, channel_id: str, bot_access_token: str, bot_user_id: str
    ) -> TwitchChatConnection: ...


class FakeTwitchChatConnection:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[datetime | None] = asyncio.Queue()
        self.closed = False

    async def push(self, timestamp: datetime) -> None:
        await self._queue.put(timestamp)

    async def end(self) -> None:
        await self._queue.put(None)

    async def receive_message(self) -> datetime | None:
        return await self._queue.get()

    async def close(self) -> None:
        self.closed = True


class FakeTwitchChatClient:
    def __init__(self) -> None:
        self.connections: dict[str, FakeTwitchChatConnection] = {}

    async def connect(
        self, channel_id: str, bot_access_token: str, bot_user_id: str
    ) -> FakeTwitchChatConnection:
        return self.connections.setdefault(channel_id, FakeTwitchChatConnection())
```

Also add `pytest-asyncio` awareness: this project already has `asyncio_mode = "auto"` configured in `pyproject.toml` (from Foundation Task 12), so `@pytest.mark.asyncio` works without further config changes.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_twitch_chat_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/twitch_chat_client.py backend/tests/test_twitch_chat_client.py
git commit -m "feat: TwitchChatClient protocol and FakeTwitchChatClient"
```

---

## Task 6: TwitchEventSubChatClient (real WebSocket implementation)

**Files:**
- Modify: `backend/app/services/twitch_chat_client.py`
- Modify: `backend/pyproject.toml` (add `websockets` dependency)
- Test: `backend/tests/test_twitch_chat_client.py`

**Interfaces:**
- Produces: `app.services.twitch_chat_client.TwitchEventSubConnection` and `TwitchEventSubChatClient(client_id: str)`, implementing `TwitchChatClient`. `connect()` opens a WebSocket to Twitch's EventSub WS endpoint, performs the welcome handshake, creates a `channel.chat.message` subscription via Helix using the transport `{"method": "websocket", "session_id": ...}`, and returns a connection whose `receive_message()` yields a timestamp per chat notification (ignoring keepalives; treating any other message type as a signal to stop — the caller's retry loop, built in Task 10, reconnects from scratch rather than this class attempting seamless migration on a `session_reconnect` message).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_twitch_chat_client.py (append)
import json
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.twitch_chat_client import TwitchEventSubChatClient


def _welcome_message() -> str:
    return json.dumps(
        {
            "metadata": {"message_type": "session_welcome"},
            "payload": {"session": {"id": "session-abc"}},
        }
    )


def _notification_message() -> str:
    return json.dumps(
        {
            "metadata": {"message_type": "notification", "subscription_type": "channel.chat.message"},
            "payload": {"event": {}},
        }
    )


def _keepalive_message() -> str:
    return json.dumps({"metadata": {"message_type": "session_keepalive"}})


@pytest.mark.asyncio
@patch("app.services.twitch_chat_client.httpx.post")
@patch("app.services.twitch_chat_client.websockets.connect")
async def test_connect_performs_welcome_handshake_and_subscribes(mock_ws_connect, mock_post):
    mock_socket = AsyncMock()
    mock_socket.recv = AsyncMock(return_value=_welcome_message())
    mock_ws_connect.return_value = mock_socket

    subscribe_response = MagicMock()
    subscribe_response.raise_for_status = MagicMock()
    mock_post.return_value = subscribe_response

    client = TwitchEventSubChatClient(client_id="cid")
    connection = await client.connect("channel-1", "bot-token", "bot-id")

    assert connection is not None
    subscribe_call = mock_post.call_args
    assert subscribe_call.kwargs["json"]["type"] == "channel.chat.message"
    assert subscribe_call.kwargs["json"]["condition"] == {
        "broadcaster_user_id": "channel-1", "user_id": "bot-id",
    }
    assert subscribe_call.kwargs["json"]["transport"] == {
        "method": "websocket", "session_id": "session-abc",
    }
    assert subscribe_call.kwargs["headers"]["Authorization"] == "Bearer bot-token"


@pytest.mark.asyncio
@patch("app.services.twitch_chat_client.httpx.post")
@patch("app.services.twitch_chat_client.websockets.connect")
async def test_receive_message_ignores_keepalive_and_yields_on_notification(mock_ws_connect, mock_post):
    mock_socket = AsyncMock()
    mock_socket.recv = AsyncMock(
        side_effect=[_welcome_message(), _keepalive_message(), _notification_message()]
    )
    mock_ws_connect.return_value = mock_socket
    mock_post.return_value = MagicMock(raise_for_status=MagicMock())

    client = TwitchEventSubChatClient(client_id="cid")
    connection = await client.connect("channel-1", "bot-token", "bot-id")
    result = await connection.receive_message()

    assert result is not None


@pytest.mark.asyncio
@patch("app.services.twitch_chat_client.httpx.post")
@patch("app.services.twitch_chat_client.websockets.connect")
async def test_receive_message_returns_none_when_connection_closes(mock_ws_connect, mock_post):
    import websockets

    mock_socket = AsyncMock()
    mock_socket.recv = AsyncMock(
        side_effect=[_welcome_message(), websockets.exceptions.ConnectionClosed(None, None)]
    )
    mock_ws_connect.return_value = mock_socket
    mock_post.return_value = MagicMock(raise_for_status=MagicMock())

    client = TwitchEventSubChatClient(client_id="cid")
    connection = await client.connect("channel-1", "bot-token", "bot-id")
    result = await connection.receive_message()

    assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pip install -e ".[dev]" && pytest tests/test_twitch_chat_client.py -v -k EventSub`
Expected: FAIL — `ModuleNotFoundError: No module named 'websockets'`, then after adding the dependency, `ImportError: cannot import name 'TwitchEventSubChatClient'`

- [ ] **Step 3: Write the implementation**

```toml
# backend/pyproject.toml — add to the main dependencies list
    "websockets>=13",
```

```python
# backend/app/services/twitch_chat_client.py (add imports and new classes)
import json
from datetime import UTC, datetime

import httpx
import websockets

_EVENTSUB_WS_URL = "wss://eventsub.wss.twitch.tv/ws"
_EVENTSUB_SUBSCRIPTIONS_URL = "https://api.twitch.tv/helix/eventsub/subscriptions"


class TwitchEventSubConnection:
    def __init__(self, websocket) -> None:
        self._websocket = websocket

    async def receive_message(self) -> datetime | None:
        while True:
            try:
                raw = await self._websocket.recv()
            except websockets.exceptions.ConnectionClosed:
                return None
            data = json.loads(raw)
            message_type = data["metadata"]["message_type"]
            if message_type == "notification":
                if data["metadata"].get("subscription_type") == "channel.chat.message":
                    return datetime.now(UTC)
                continue
            # session_keepalive, session_reconnect, revocation, or anything
            # else: ignore and keep reading. A session_reconnect isn't
            # migrated seamlessly here — the caller's retry loop (Task 10)
            # reconnects from scratch once this connection eventually drops.
            continue

    async def close(self) -> None:
        await self._websocket.close()


class TwitchEventSubChatClient:
    def __init__(self, client_id: str) -> None:
        self._client_id = client_id

    async def connect(
        self, channel_id: str, bot_access_token: str, bot_user_id: str
    ) -> TwitchEventSubConnection:
        websocket = await websockets.connect(_EVENTSUB_WS_URL)
        welcome_raw = await websocket.recv()
        welcome = json.loads(welcome_raw)
        session_id = welcome["payload"]["session"]["id"]

        response = httpx.post(
            _EVENTSUB_SUBSCRIPTIONS_URL,
            headers={"Client-Id": self._client_id, "Authorization": f"Bearer {bot_access_token}"},
            json={
                "type": "channel.chat.message",
                "version": "1",
                "condition": {"broadcaster_user_id": channel_id, "user_id": bot_user_id},
                "transport": {"method": "websocket", "session_id": session_id},
            },
            timeout=10.0,
        )
        response.raise_for_status()

        return TwitchEventSubConnection(websocket)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_twitch_chat_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/twitch_chat_client.py backend/pyproject.toml \
        backend/tests/test_twitch_chat_client.py
git commit -m "feat: TwitchEventSubChatClient real WebSocket implementation"
```

---

## Task 7: YouTubeChatClient protocol + Fake + real YouTubeChatAPIClient

**Files:**
- Create: `backend/app/services/youtube_chat_client.py`
- Test: `backend/tests/test_youtube_chat_client.py`

**Interfaces:**
- Produces: `ChatPollResult` (dataclass: `message_count: int`, `next_page_token: str | None`). Produces `YouTubeChatClient` (Protocol: `get_live_chat_id(video_id: str) -> str | None`, `count_new_messages(live_chat_id: str, page_token: str | None) -> ChatPollResult`). Produces `FakeYouTubeChatClient` (`.live_chat_ids: dict[str, str]`, `.poll_results: dict[str, ChatPollResult]`). Produces `YouTubeChatAPIClient(api_key: str)` — sends the API key via the `X-goog-api-key` header (never a URL query param, per the global constraint carried forward from Stream Discovery's Critical finding).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_youtube_chat_client.py
from unittest.mock import MagicMock, patch

from app.services.youtube_chat_client import ChatPollResult, FakeYouTubeChatClient, YouTubeChatAPIClient


def test_fake_client_returns_configured_values():
    client = FakeYouTubeChatClient()
    client.live_chat_ids["video-1"] = "chat-1"
    client.poll_results["chat-1"] = ChatPollResult(message_count=3, next_page_token="next-1")

    assert client.get_live_chat_id("video-1") == "chat-1"
    assert client.get_live_chat_id("video-2") is None
    assert client.count_new_messages("chat-1", None) == ChatPollResult(message_count=3, next_page_token="next-1")


@patch("app.services.youtube_chat_client.httpx.get")
def test_get_live_chat_id_sends_key_as_header_not_query_param(mock_get):
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"items": [{"liveStreamingDetails": {"activeLiveChatId": "chat-1"}}]}
    mock_get.return_value = response

    client = YouTubeChatAPIClient(api_key="secret-key")
    result = client.get_live_chat_id("video-1")

    assert result == "chat-1"
    call_kwargs = mock_get.call_args.kwargs
    assert call_kwargs["headers"]["X-goog-api-key"] == "secret-key"
    assert "key" not in call_kwargs["params"]


@patch("app.services.youtube_chat_client.httpx.get")
def test_get_live_chat_id_returns_none_when_not_live(mock_get):
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"items": []}
    mock_get.return_value = response

    client = YouTubeChatAPIClient(api_key="secret-key")
    assert client.get_live_chat_id("video-1") is None


@patch("app.services.youtube_chat_client.httpx.get")
def test_count_new_messages_sends_key_as_header_and_returns_count(mock_get):
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {"items": [{}, {}, {}], "nextPageToken": "page-2"}
    mock_get.return_value = response

    client = YouTubeChatAPIClient(api_key="secret-key")
    result = client.count_new_messages("chat-1", page_token="page-1")

    assert result.message_count == 3
    assert result.next_page_token == "page-2"
    call_kwargs = mock_get.call_args.kwargs
    assert call_kwargs["headers"]["X-goog-api-key"] == "secret-key"
    assert "key" not in call_kwargs["params"]
    assert call_kwargs["params"]["pageToken"] == "page-1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_youtube_chat_client.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.youtube_chat_client'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/services/youtube_chat_client.py
from dataclasses import dataclass
from typing import Protocol

import httpx

_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
_LIVE_CHAT_MESSAGES_URL = "https://www.googleapis.com/youtube/v3/liveChat/messages"


@dataclass
class ChatPollResult:
    message_count: int
    next_page_token: str | None


class YouTubeChatClient(Protocol):
    def get_live_chat_id(self, video_id: str) -> str | None: ...
    def count_new_messages(self, live_chat_id: str, page_token: str | None) -> ChatPollResult: ...


class FakeYouTubeChatClient:
    def __init__(self) -> None:
        self.live_chat_ids: dict[str, str] = {}
        self.poll_results: dict[str, ChatPollResult] = {}

    def get_live_chat_id(self, video_id: str) -> str | None:
        return self.live_chat_ids.get(video_id)

    def count_new_messages(self, live_chat_id: str, page_token: str | None) -> ChatPollResult:
        return self.poll_results.get(live_chat_id, ChatPollResult(message_count=0, next_page_token=None))


class YouTubeChatAPIClient:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    def get_live_chat_id(self, video_id: str) -> str | None:
        response = httpx.get(
            _VIDEOS_URL,
            params={"part": "liveStreamingDetails", "id": video_id},
            headers={"X-goog-api-key": self._api_key},
            timeout=10.0,
        )
        response.raise_for_status()
        items = response.json()["items"]
        if not items:
            return None
        return items[0].get("liveStreamingDetails", {}).get("activeLiveChatId")

    def count_new_messages(self, live_chat_id: str, page_token: str | None) -> ChatPollResult:
        params: dict[str, str] = {"liveChatId": live_chat_id, "part": "id"}
        if page_token:
            params["pageToken"] = page_token
        response = httpx.get(
            _LIVE_CHAT_MESSAGES_URL,
            params=params,
            headers={"X-goog-api-key": self._api_key},
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
        return ChatPollResult(
            message_count=len(payload.get("items", [])),
            next_page_token=payload.get("nextPageToken"),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_youtube_chat_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/youtube_chat_client.py backend/tests/test_youtube_chat_client.py
git commit -m "feat: YouTubeChatClient protocol, Fake, and real YouTubeChatAPIClient"
```

---

## Task 8: record_chat_activity + YouTube poll-state helpers

**Files:**
- Create: `backend/app/services/clip_detection_service.py`
- Test: `backend/tests/test_clip_detection_service.py`

**Interfaces:**
- Consumes: `app.models.chat_activity.ChatActivityBucket`, `YouTubeChatPollState` (Task 1)
- Produces: `record_chat_activity(db: Session, stream_session_id: int, bucket_start: datetime, message_count: int) -> None` (creates a bucket, or adds to an existing one at that exact `bucket_start` — upsert-by-add semantics, so two calls for the same bucket accumulate rather than overwrite). Produces `get_or_create_poll_state(db: Session, stream_session_id: int) -> YouTubeChatPollState`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_clip_detection_service.py
from datetime import UTC, datetime

from app.models.chat_activity import ChatActivityBucket
from app.models.creator import Creator
from app.models.stream_session import StreamSession
from app.services.clip_detection_service import get_or_create_poll_state, record_chat_activity


def _make_session(db_session):
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    session = StreamSession(
        creator_id=creator.id, platform="twitch", external_stream_id="s1", started_at=datetime.now(UTC)
    )
    db_session.add(session)
    db_session.commit()
    return session


def test_record_chat_activity_creates_bucket(db_session):
    session = _make_session(db_session)
    bucket_start = datetime.now(UTC)
    record_chat_activity(db_session, session.id, bucket_start, 5)

    bucket = db_session.query(ChatActivityBucket).filter(ChatActivityBucket.stream_session_id == session.id).first()
    assert bucket.message_count == 5


def test_record_chat_activity_accumulates_on_same_bucket(db_session):
    session = _make_session(db_session)
    bucket_start = datetime.now(UTC)
    record_chat_activity(db_session, session.id, bucket_start, 3)
    record_chat_activity(db_session, session.id, bucket_start, 4)

    buckets = db_session.query(ChatActivityBucket).filter(ChatActivityBucket.stream_session_id == session.id).all()
    assert len(buckets) == 1
    assert buckets[0].message_count == 7


def test_get_or_create_poll_state_creates_once(db_session):
    session = _make_session(db_session)
    state_a = get_or_create_poll_state(db_session, session.id)
    state_a.next_page_token = "abc"
    db_session.commit()

    state_b = get_or_create_poll_state(db_session, session.id)
    assert state_b.next_page_token == "abc"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_clip_detection_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.clip_detection_service'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/services/clip_detection_service.py
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.chat_activity import ChatActivityBucket, YouTubeChatPollState


def record_chat_activity(
    db: Session, stream_session_id: int, bucket_start: datetime, message_count: int
) -> None:
    existing = (
        db.query(ChatActivityBucket)
        .filter(
            ChatActivityBucket.stream_session_id == stream_session_id,
            ChatActivityBucket.bucket_start == bucket_start,
        )
        .first()
    )
    if existing is not None:
        existing.message_count += message_count
    else:
        db.add(
            ChatActivityBucket(
                stream_session_id=stream_session_id, bucket_start=bucket_start, message_count=message_count
            )
        )
    db.commit()


def get_or_create_poll_state(db: Session, stream_session_id: int) -> YouTubeChatPollState:
    state = db.get(YouTubeChatPollState, stream_session_id)
    if state is None:
        state = YouTubeChatPollState(stream_session_id=stream_session_id, next_page_token=None)
        db.add(state)
        db.commit()
        db.refresh(state)
    return state
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_clip_detection_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/clip_detection_service.py backend/tests/test_clip_detection_service.py
git commit -m "feat: record_chat_activity and YouTube poll-state helpers"
```

---

## Task 9: Clip candidate detection algorithm

**Files:**
- Modify: `backend/app/services/clip_detection_service.py`
- Modify: `backend/app/core/config.py` (add detection settings)
- Modify: `backend/tests/test_clip_detection_service.py`

**Interfaces:**
- Consumes: `app.models.chat_activity.ChatActivityBucket`, `ClipCandidate` (Task 1); `app.models.stream_session.StreamSession` (Stream Discovery)
- Produces: `detect_clip_candidates_for_session(db: Session, session: StreamSession, *, z_threshold: float, min_gap_seconds: int, lookback_minutes: int = 10) -> list[ClipCandidate]`.
- Adds `clip_detection_z_threshold: float = 2.0`, `clip_detection_min_gap_seconds: int = 30`, `twitch_chat_bucket_seconds: int = 15`, `youtube_chat_poll_interval_minutes: int = 5` to `Settings`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_clip_detection_service.py (append)
from datetime import timedelta

from app.models.chat_activity import ClipCandidate
from app.services.clip_detection_service import detect_clip_candidates_for_session


def _add_bucket(db_session, session, start, count):
    from app.services.clip_detection_service import record_chat_activity

    record_chat_activity(db_session, session.id, start, count)


def test_no_spike_produces_no_candidate(db_session):
    session = _make_session(db_session)
    base = datetime.now(UTC)
    for i in range(10):
        _add_bucket(db_session, session, base + timedelta(seconds=15 * i), 5)

    candidates = detect_clip_candidates_for_session(db_session, session, z_threshold=2.0, min_gap_seconds=30)
    assert candidates == []


def test_single_clear_spike_produces_one_candidate(db_session):
    session = _make_session(db_session)
    base = datetime.now(UTC)
    counts = [5, 5, 5, 5, 5, 60, 5, 5, 5, 5]
    for i, count in enumerate(counts):
        _add_bucket(db_session, session, base + timedelta(seconds=15 * i), count)

    candidates = detect_clip_candidates_for_session(db_session, session, z_threshold=2.0, min_gap_seconds=30)

    assert len(candidates) == 1
    assert candidates[0].signal_type == "chat_spike"
    assert candidates[0].start_at <= base + timedelta(seconds=15 * 5)
    assert candidates[0].end_at >= base + timedelta(seconds=15 * 6)


def test_adjacent_spikes_merge_into_one_candidate(db_session):
    session = _make_session(db_session)
    base = datetime.now(UTC)
    counts = [5, 5, 5, 60, 65, 5, 5, 5]
    for i, count in enumerate(counts):
        _add_bucket(db_session, session, base + timedelta(seconds=15 * i), count)

    candidates = detect_clip_candidates_for_session(db_session, session, z_threshold=2.0, min_gap_seconds=30)
    assert len(candidates) == 1


def test_far_apart_spikes_produce_two_candidates(db_session):
    session = _make_session(db_session)
    base = datetime.now(UTC)
    counts = [5, 60, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 60, 5]
    for i, count in enumerate(counts):
        _add_bucket(db_session, session, base + timedelta(seconds=15 * i), count)

    candidates = detect_clip_candidates_for_session(db_session, session, z_threshold=2.0, min_gap_seconds=30)
    assert len(candidates) == 2


def test_rerunning_detection_does_not_duplicate_candidates(db_session):
    session = _make_session(db_session)
    base = datetime.now(UTC)
    counts = [5, 5, 5, 5, 5, 60, 5, 5, 5, 5]
    for i, count in enumerate(counts):
        _add_bucket(db_session, session, base + timedelta(seconds=15 * i), count)

    first_run = detect_clip_candidates_for_session(db_session, session, z_threshold=2.0, min_gap_seconds=30)
    second_run = detect_clip_candidates_for_session(db_session, session, z_threshold=2.0, min_gap_seconds=30)

    assert len(first_run) == 1
    assert second_run == []
    assert db_session.query(ClipCandidate).filter(ClipCandidate.stream_session_id == session.id).count() == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_clip_detection_service.py -v -k detect`
Expected: FAIL — `ImportError: cannot import name 'detect_clip_candidates_for_session'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/core/config.py — add these fields to the Settings class
    clip_detection_z_threshold: float = 2.0
    clip_detection_min_gap_seconds: int = 30
    twitch_chat_bucket_seconds: int = 15
    youtube_chat_poll_interval_minutes: int = 5
```

```python
# backend/app/services/clip_detection_service.py (add)
import statistics
from datetime import UTC, datetime, timedelta

from app.models.chat_activity import ChatActivityBucket, ClipCandidate
from app.models.stream_session import StreamSession

_PADDING_BEFORE_SECONDS = 5
_PADDING_AFTER_SECONDS = 10


def _bucket_width_seconds(platform: str) -> int:
    from app.core.config import get_settings

    settings = get_settings()
    if platform == "twitch":
        return settings.twitch_chat_bucket_seconds
    return settings.youtube_chat_poll_interval_minutes * 60


def detect_clip_candidates_for_session(
    db: Session,
    session: StreamSession,
    *,
    z_threshold: float,
    min_gap_seconds: int,
    lookback_minutes: int = 10,
) -> list[ClipCandidate]:
    cutoff = datetime.now(UTC) - timedelta(minutes=lookback_minutes)
    buckets = (
        db.query(ChatActivityBucket)
        .filter(ChatActivityBucket.stream_session_id == session.id, ChatActivityBucket.bucket_start >= cutoff)
        .order_by(ChatActivityBucket.bucket_start)
        .all()
    )
    if len(buckets) < 2:
        return []

    counts = [b.message_count for b in buckets]
    mean = statistics.mean(counts)
    stdev = statistics.pstdev(counts)
    if stdev == 0:
        return []

    hot = [(c - mean) / stdev >= z_threshold for c in counts]
    bucket_width = _bucket_width_seconds(session.platform)

    created: list[ClipCandidate] = []
    i = 0
    while i < len(buckets):
        if not hot[i]:
            i += 1
            continue

        start_idx = end_idx = i
        peak_z = (counts[i] - mean) / stdev
        j = i + 1
        while j < len(buckets):
            gap = (buckets[j].bucket_start - buckets[end_idx].bucket_start).total_seconds() - bucket_width
            if hot[j] or gap <= min_gap_seconds:
                if hot[j]:
                    end_idx = j
                    peak_z = max(peak_z, (counts[j] - mean) / stdev)
                j += 1
            else:
                break

        window_start = buckets[start_idx].bucket_start - timedelta(seconds=_PADDING_BEFORE_SECONDS)
        window_end = buckets[end_idx].bucket_start + timedelta(
            seconds=bucket_width + _PADDING_AFTER_SECONDS
        )

        overlapping = (
            db.query(ClipCandidate)
            .filter(
                ClipCandidate.stream_session_id == session.id,
                ClipCandidate.start_at < window_end,
                ClipCandidate.end_at > window_start,
            )
            .first()
        )
        if overlapping is None:
            candidate = ClipCandidate(
                stream_session_id=session.id,
                start_at=window_start,
                end_at=window_end,
                score=peak_z,
                signal_type="chat_spike",
            )
            db.add(candidate)
            created.append(candidate)

        i = end_idx + 1

    db.commit()
    return created
```

Note: `Session` needs importing (`from sqlalchemy.orm import Session`) — add alongside the other imports at the top of the file if not already present from Task 8.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_clip_detection_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/clip_detection_service.py backend/app/core/config.py \
        backend/tests/test_clip_detection_service.py
git commit -m "feat: chat-spike clip candidate detection algorithm"
```

---

## Task 10: listen_to_twitch_chat arq job

**Files:**
- Create: `backend/app/workers/chat_tasks.py`
- Test: `backend/tests/test_chat_tasks.py`

**Interfaces:**
- Consumes: `TwitchChatClient`, `TwitchEventSubChatClient` (Tasks 5-6); `get_valid_bot_access_token`, `get_bot_user_id` (Task 4); `record_chat_activity` (Task 8); `is_authorized` (Foundation)
- Produces: `async def listen_to_twitch_chat(ctx: dict, creator_id: int, stream_session_id: int, chat_client: TwitchChatClient | None = None) -> None`.
- Adds `chat_listener_max_duration_hours: int = 12` to `Settings`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_chat_tasks.py
from datetime import UTC, datetime, timedelta

import pytest

from app.models.agreement import Agreement, AgreementStatus, AgreementTermsVersion
from app.models.chat_activity import ChatActivityBucket
from app.models.creator import Creator, CreatorStatus
from app.models.stream_session import StreamSession
from app.services.twitch_chat_client import FakeTwitchChatClient
from app.workers.chat_tasks import listen_to_twitch_chat


class _NonClosingSessionProxy:
    def __init__(self, session):
        self._session = session

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._session, name)


def _authorized_creator(db_session):
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


@pytest.mark.asyncio
async def test_listener_buckets_messages_and_stops_when_session_ends(db_session, monkeypatch):
    monkeypatch.setattr("app.workers.chat_tasks.SessionLocal", lambda: _NonClosingSessionProxy(db_session))
    creator = _authorized_creator(db_session)
    session = StreamSession(
        creator_id=creator.id, platform="twitch", external_stream_id="s1", started_at=datetime.now(UTC)
    )
    db_session.add(session)
    db_session.commit()

    fake_client = FakeTwitchChatClient()
    connection = await fake_client.connect("channel-1", "token", "bot-id")
    now = datetime.now(UTC)
    await connection.push(now)
    await connection.push(now + timedelta(seconds=1))

    async def _end_session_then_close():
        db_session.refresh(session)
        session.ended_at = datetime.now(UTC)
        db_session.commit()
        await connection.end()

    import asyncio

    asyncio.create_task(_end_session_then_close())

    await listen_to_twitch_chat({}, creator.id, session.id, chat_client=fake_client)

    buckets = db_session.query(ChatActivityBucket).filter(ChatActivityBucket.stream_session_id == session.id).all()
    assert sum(b.message_count for b in buckets) >= 1


@pytest.mark.asyncio
async def test_listener_stops_when_creator_revoked_mid_run(db_session, monkeypatch):
    from sqlalchemy import text

    monkeypatch.setattr("app.workers.chat_tasks.SessionLocal", lambda: _NonClosingSessionProxy(db_session))
    creator = _authorized_creator(db_session)
    session = StreamSession(
        creator_id=creator.id, platform="twitch", external_stream_id="s1", started_at=datetime.now(UTC)
    )
    db_session.add(session)
    db_session.commit()

    fake_client = FakeTwitchChatClient()
    connection = await fake_client.connect("channel-1", "token", "bot-id")
    await connection.push(datetime.now(UTC))

    async def _revoke_via_raw_sql_then_close():
        db_session.execute(text("UPDATE creators SET status = :status WHERE id = :id"), {
            "status": "revoked", "id": creator.id,
        })
        db_session.commit()
        await connection.end()

    import asyncio

    asyncio.create_task(_revoke_via_raw_sql_then_close())

    await listen_to_twitch_chat({}, creator.id, session.id, chat_client=fake_client)

    assert connection.closed is True
```

Note: this task requires `get_valid_bot_access_token`/`get_bot_user_id` to succeed even though no real `TwitchBotCredential` is linked in tests — since `chat_client` is passed explicitly as a `FakeTwitchChatClient` (which ignores the token/bot_id it's given), the bot-credential lookup only needs to not crash before reaching the fake. Either seed a `TwitchBotCredential` row in these tests' setup, or (cleaner) have `listen_to_twitch_chat` accept the token/bot_id as already-resolved when `chat_client` is injected for testing — use your judgment on the cleanest way to make this testable without over-complicating the production code path; document whichever choice you make in your task report.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_chat_tasks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.workers.chat_tasks'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/core/config.py — add this field to the Settings class
    chat_listener_max_duration_hours: int = 12
```

```python
# backend/app/workers/chat_tasks.py
import asyncio
import logging
from datetime import UTC, datetime, timedelta

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.creator import Creator
from app.models.stream_session import StreamSession
from app.services.clip_detection_service import record_chat_activity
from app.services.permission_gate import is_authorized
from app.services.twitch_bot_service import get_bot_user_id, get_valid_bot_access_token
from app.services.twitch_chat_client import TwitchChatClient, TwitchEventSubChatClient

logger = logging.getLogger(__name__)


async def listen_to_twitch_chat(
    ctx: dict,
    creator_id: int,
    stream_session_id: int,
    chat_client: TwitchChatClient | None = None,
) -> None:
    settings = get_settings()
    db = SessionLocal()
    try:
        creator = db.get(Creator, creator_id)
        if creator is None or not is_authorized(db, creator_id):
            return

        client = chat_client or TwitchEventSubChatClient(client_id=settings.twitch_client_id)
        bot_token = get_valid_bot_access_token(db)
        bot_user_id = get_bot_user_id(db)
        connection = await client.connect(creator.platform_channel_id, bot_token, bot_user_id)

        bucket_seconds = settings.twitch_chat_bucket_seconds
        deadline = datetime.now(UTC) + timedelta(hours=settings.chat_listener_max_duration_hours)
        current_bucket_start: datetime | None = None
        current_bucket_count = 0

        def _flush() -> None:
            nonlocal current_bucket_start, current_bucket_count
            if current_bucket_start is not None and current_bucket_count > 0:
                record_chat_activity(db, stream_session_id, current_bucket_start, current_bucket_count)
            current_bucket_count = 0

        try:
            while True:
                if datetime.now(UTC) >= deadline:
                    break

                session = db.get(StreamSession, stream_session_id)
                if session is not None and session.ended_at is not None:
                    break

                db.expire(creator)
                if not is_authorized(db, creator_id):
                    break

                timestamp = await connection.receive_message()
                if timestamp is None:
                    break

                bucket_start = timestamp.replace(
                    second=(timestamp.second // bucket_seconds) * bucket_seconds, microsecond=0
                )
                if current_bucket_start is None:
                    current_bucket_start = bucket_start
                elif bucket_start != current_bucket_start:
                    _flush()
                    current_bucket_start = bucket_start
                current_bucket_count += 1
        finally:
            _flush()
            await connection.close()
    finally:
        db.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_chat_tasks.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/workers/chat_tasks.py backend/app/core/config.py backend/tests/test_chat_tasks.py
git commit -m "feat: listen_to_twitch_chat long-running arq job"
```

---

## Task 11: poll_youtube_chat arq cron job

**Files:**
- Modify: `backend/app/workers/chat_tasks.py`
- Modify: `backend/tests/test_chat_tasks.py`

**Interfaces:**
- Consumes: `YouTubeChatClient` (Task 7); `record_chat_activity`, `get_or_create_poll_state` (Task 8); `list_authorized_creators` (Stream Discovery)
- Produces: `async def poll_youtube_chat(ctx: dict) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_chat_tasks.py (append)
from app.services.stream_discovery_service import reconcile_creator_stream_state
from app.services.stream_info import StreamInfo
from app.services.youtube_chat_client import ChatPollResult, FakeYouTubeChatClient
from app.workers.chat_tasks import poll_youtube_chat


@pytest.mark.asyncio
async def test_poll_youtube_chat_records_bucket_for_live_creator(db_session, monkeypatch):
    monkeypatch.setattr("app.workers.chat_tasks.SessionLocal", lambda: _NonClosingSessionProxy(db_session))

    terms = AgreementTermsVersion(version="v1", effective_date=datetime.now(UTC).date(), body_markdown="x")
    db_session.add(terms)
    db_session.commit()
    creator = Creator(
        platform="youtube", platform_channel_id="yt-1", display_name="A", status=CreatorStatus.AUTHORIZED
    )
    db_session.add(creator)
    db_session.commit()
    db_session.add(
        Agreement(creator_id=creator.id, terms_version_id=terms.id, rev_share_pct=50.0, status=AgreementStatus.ACTIVE)
    )
    db_session.commit()
    reconcile_creator_stream_state(
        db_session, creator,
        StreamInfo(external_stream_id="video-1", title="t", category=None, viewer_count=1, started_at=datetime.now(UTC)),
    )

    fake_client = FakeYouTubeChatClient()
    fake_client.live_chat_ids["video-1"] = "chat-1"
    fake_client.poll_results["chat-1"] = ChatPollResult(message_count=7, next_page_token="next-token")
    monkeypatch.setattr("app.workers.chat_tasks.YouTubeChatAPIClient", lambda api_key: fake_client)

    await poll_youtube_chat({})

    from app.models.chat_activity import ChatActivityBucket, YouTubeChatPollState
    from app.models.stream_session import StreamSession

    session = db_session.query(StreamSession).filter(StreamSession.creator_id == creator.id).first()
    buckets = db_session.query(ChatActivityBucket).filter(ChatActivityBucket.stream_session_id == session.id).all()
    assert sum(b.message_count for b in buckets) == 7

    state = db_session.get(YouTubeChatPollState, session.id)
    assert state.next_page_token == "next-token"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_chat_tasks.py -v -k poll_youtube_chat`
Expected: FAIL — `ImportError: cannot import name 'poll_youtube_chat'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/workers/chat_tasks.py (add)
from datetime import UTC, datetime

from app.models.stream_session import StreamSession
from app.services.clip_detection_service import get_or_create_poll_state
from app.services.stream_discovery_service import list_authorized_creators
from app.services.youtube_chat_client import YouTubeChatAPIClient


async def poll_youtube_chat(ctx: dict) -> None:
    settings = get_settings()
    client = YouTubeChatAPIClient(api_key=settings.youtube_api_key)
    db = SessionLocal()
    try:
        for creator in list_authorized_creators(db, platform="youtube"):
            session = (
                db.query(StreamSession)
                .filter(StreamSession.creator_id == creator.id, StreamSession.ended_at.is_(None))
                .first()
            )
            if session is None:
                continue
            try:
                live_chat_id = client.get_live_chat_id(session.external_stream_id)
                if live_chat_id is None:
                    continue
                state = get_or_create_poll_state(db, session.id)
                result = client.count_new_messages(live_chat_id, state.next_page_token)
                if result.message_count > 0:
                    record_chat_activity(db, session.id, datetime.now(UTC), result.message_count)
                state.next_page_token = result.next_page_token
                db.commit()
            except Exception:
                db.rollback()
                logger.exception(
                    "Failed to poll YouTube chat for creator %s (channel %s)",
                    creator.id, creator.platform_channel_id,
                )
                continue
    finally:
        db.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_chat_tasks.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/workers/chat_tasks.py backend/tests/test_chat_tasks.py
git commit -m "feat: poll_youtube_chat arq cron job"
```

---

## Task 12: detect_clip_candidates arq cron job + WorkerSettings wiring

**Files:**
- Modify: `backend/app/workers/chat_tasks.py`
- Modify: `backend/app/workers/settings.py`
- Modify: `backend/tests/test_chat_tasks.py`

**Interfaces:**
- Consumes: `detect_clip_candidates_for_session` (Task 9); `list_authorized_creators` (Stream Discovery)
- Produces: `async def detect_clip_candidates(ctx: dict) -> None`. Modifies `WorkerSettings.cron_jobs` to add `poll_youtube_chat`, `detect_clip_candidates`, and registers `listen_to_twitch_chat` in `WorkerSettings.functions` (it's enqueued on-demand, not scheduled, so it belongs in `functions` not `cron_jobs`).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_chat_tasks.py (append)
from app.workers.chat_tasks import detect_clip_candidates


@pytest.mark.asyncio
async def test_detect_clip_candidates_scans_open_sessions_and_stores_candidates(db_session, monkeypatch):
    monkeypatch.setattr("app.workers.chat_tasks.SessionLocal", lambda: _NonClosingSessionProxy(db_session))
    creator = _authorized_creator(db_session)
    session = StreamSession(
        creator_id=creator.id, platform="twitch", external_stream_id="s1", started_at=datetime.now(UTC)
    )
    db_session.add(session)
    db_session.commit()

    base = datetime.now(UTC)
    counts = [5, 5, 5, 5, 5, 60, 5, 5, 5, 5]
    for i, count in enumerate(counts):
        record_chat_activity(db_session, session.id, base + timedelta(seconds=15 * i), count)

    await detect_clip_candidates({})

    from app.models.chat_activity import ClipCandidate

    candidates = db_session.query(ClipCandidate).filter(ClipCandidate.stream_session_id == session.id).all()
    assert len(candidates) == 1
```

(This test needs `record_chat_activity` and `timedelta` imported at the top of `test_chat_tasks.py` — add those imports alongside the existing ones.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_chat_tasks.py -v -k detect_clip_candidates`
Expected: FAIL — `ImportError: cannot import name 'detect_clip_candidates'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/workers/chat_tasks.py (add)
from app.models.stream_session import StreamSession as StreamSessionModel  # already imported above; reuse existing import
from app.services.clip_detection_service import detect_clip_candidates_for_session


async def detect_clip_candidates(ctx: dict) -> None:
    settings = get_settings()
    db = SessionLocal()
    try:
        open_sessions = db.query(StreamSession).filter(StreamSession.ended_at.is_(None)).all()
        for session in open_sessions:
            creator = db.get(Creator, session.creator_id)
            if creator is None or not is_authorized(db, creator.id):
                continue
            try:
                detect_clip_candidates_for_session(
                    db,
                    session,
                    z_threshold=settings.clip_detection_z_threshold,
                    min_gap_seconds=settings.clip_detection_min_gap_seconds,
                )
            except Exception:
                db.rollback()
                logger.exception("Failed to detect clip candidates for session %s", session.id)
                continue
    finally:
        db.close()
```

(Remove the redundant duplicate `StreamSession` import line above if your editor/linter flags it — `StreamSession` is already imported earlier in this file from Task 11; just reuse it directly, no alias needed.)

```python
# backend/app/workers/settings.py (replace entire file)
from arq.connections import RedisSettings
from arq.cron import cron

from app.core.config import get_settings
from app.workers.chat_tasks import detect_clip_candidates, listen_to_twitch_chat, poll_youtube_chat
from app.workers.stream_discovery_tasks import (
    poll_twitch_streams_backup,
    poll_youtube_streams,
    reconcile_twitch_subscriptions_task,
)
from app.workers.tasks import send_approved_outreach_email


class WorkerSettings:
    functions = [send_approved_outreach_email, listen_to_twitch_chat]
    cron_jobs = [
        cron(poll_youtube_streams, minute=set(range(0, 60, 5))),
        cron(poll_twitch_streams_backup, minute=set(range(0, 60, 15))),
        cron(reconcile_twitch_subscriptions_task, minute=set(range(0, 60, 30))),
        cron(poll_youtube_chat, minute=set(range(0, 60, 5))),
        cron(detect_clip_candidates, minute=set(range(0, 60, 2))),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && pytest tests/test_chat_tasks.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/workers/chat_tasks.py backend/app/workers/settings.py backend/tests/test_chat_tasks.py
git commit -m "feat: detect_clip_candidates cron job and WorkerSettings wiring"
```

---

## Task 13: Webhook integration, docs

**Files:**
- Modify: `backend/app/api/webhooks.py`
- Modify: `backend/app/main.py`
- Modify: `.env.example`
- Modify: `README.md`
- Test: `backend/tests/test_webhooks.py`

**Interfaces:**
- Modifies the existing `stream.online` handling in `app.api.webhooks` to also enqueue `listen_to_twitch_chat` for the creator/session. Modifies `app.main.app` to include `app.api.twitch_bot_oauth.router`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_webhooks.py (append)
from unittest.mock import AsyncMock, patch


@patch("app.api.webhooks.enqueue_listen_to_twitch_chat")
@patch("app.api.webhooks.TwitchAPIClient")
def test_webhook_stream_online_enqueues_chat_listener(mock_client_cls, mock_enqueue, db_session, monkeypatch):
    monkeypatch.setenv("TWITCH_WEBHOOK_SECRET", WEBHOOK_SECRET)
    from app.core.config import get_settings

    get_settings.cache_clear()
    mock_enqueue.return_value = AsyncMock()()
    creator = _authorized_twitch_creator(db_session)
    mock_client_cls.return_value.get_stream_status.return_value = StreamInfo(
        external_stream_id="stream-1", title="t", category="c", viewer_count=10, started_at=datetime.now(UTC)
    )
    app = _make_app(db_session)
    client = TestClient(app)

    body = json.dumps(
        {"subscription": {"type": "stream.online"}, "event": {"broadcaster_user_id": "channel-1"}}
    ).encode()
    headers = _sign(body) | {TYPE_HEADER: "notification"}
    response = client.post("/webhooks/twitch/eventsub", content=body, headers=headers)

    assert response.status_code == 200
    mock_enqueue.assert_called_once()
    call_args = mock_enqueue.call_args
    assert call_args.kwargs["creator_id"] == creator.id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && pytest tests/test_webhooks.py -v -k enqueues_chat_listener`
Expected: FAIL — `AttributeError: <module 'app.api.webhooks'> does not have the attribute 'enqueue_listen_to_twitch_chat'`

- [ ] **Step 3: Write the implementation**

```python
# backend/app/workers/queue.py (add alongside the existing enqueue_send_outreach_email)
async def enqueue_listen_to_twitch_chat(*, creator_id: int, stream_session_id: int) -> None:
    pool = await _get_pool()
    await pool.enqueue_job("listen_to_twitch_chat", creator_id, stream_session_id)
```

```python
# backend/app/api/webhooks.py — modify the stream.online branch
from app.workers.queue import enqueue_listen_to_twitch_chat, enqueue_send_outreach_email  # adjust import list as needed
from app.models.stream_session import StreamSession

# inside the "notification" branch, replace the stream.online handling with:
        if subscription_type == "stream.online":
            stream_info = client.get_stream_status(channel_id)
            if stream_info is not None:
                reconcile_creator_stream_state(db, creator, stream_info)
                session = (
                    db.query(StreamSession)
                    .filter(StreamSession.creator_id == creator.id, StreamSession.ended_at.is_(None))
                    .first()
                )
                if session is not None:
                    await enqueue_listen_to_twitch_chat(creator_id=creator.id, stream_session_id=session.id)
        elif subscription_type == "stream.offline":
            reconcile_creator_stream_state(db, creator, None)
```

```python
# backend/app/main.py — add the import and include_router call
from app.api.twitch_bot_oauth import router as twitch_bot_oauth_router

# after app.include_router(webhooks_router):
app.include_router(twitch_bot_oauth_router)
```

```bash
# .env.example — append
TWITCH_BOT_OAUTH_REDIRECT_URI=https://your-deployed-domain.example/internal/twitch-bot/callback
CLIP_DETECTION_Z_THRESHOLD=2.0
CLIP_DETECTION_MIN_GAP_SECONDS=30
TWITCH_CHAT_BUCKET_SECONDS=15
YOUTUBE_CHAT_POLL_INTERVAL_MINUTES=5
CHAT_LISTENER_MAX_DURATION_HOURS=12
```

README.md — append a new section:

```markdown
## Clip Detection (sub-project 3)

Detects clip-worthy moments from chat-activity spikes during authorized
creators' livestreams. Twitch chat is read in real time via an EventSub
WebSocket; YouTube chat is polled (adding to the same quota constraint noted
in the Stream Discovery section above).

**One-time setup — link a Twitch bot account:** reading Twitch chat requires
a *user* access token, not the app-only token used elsewhere in this repo.
Create a dedicated Twitch account for this (e.g. `streamclipco_bot`), log
into it in a browser, then — while also logged into an internal admin
account in the same browser — visit
`https://your-deployed-domain.example/internal/twitch-bot/authorize`. This
redirects to Twitch, where you approve access from the *bot* account, then
redirects back and stores the bot's access/refresh token. The token refreshes
itself automatically thereafter; you only do this once (or again if the bot
account's authorization is ever revoked on Twitch's side).

Until this is linked, `listen_to_twitch_chat` jobs will fail immediately with
a clear "No Twitch bot account linked yet" error — safe, but no Twitch chat
data will be collected.
```

- [ ] **Step 4: Run test, then full suite**

Run: `cd backend && pytest tests/test_webhooks.py -v -k enqueues_chat_listener` → Expected: PASS
Run: `cd backend && pytest -v` → Expected: all PASS
Run: `cd backend && ruff check . && mypy app` → Expected: both clean

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/webhooks.py backend/app/main.py backend/app/workers/queue.py \
        .env.example README.md backend/tests/test_webhooks.py
git commit -m "feat: enqueue chat listener on stream.online, wire OAuth router, docs"
```

---

## Self-Review Notes

- **Spec coverage:** every piece from the design spec maps to a task — data models (Tasks 1-2), the OAuth bot-linking flow (Tasks 3-4), platform chat clients (Tasks 5-7), bucket-writing and the detection algorithm (Tasks 8-9), the three worker entry points (Tasks 10-12), and webhook integration/docs (Task 13).
- **Lessons from Stream Discovery's final review carried forward explicitly:** the chat listener's per-flush `db.expire(creator)` re-check (Task 10) directly reuses the fix for that review's Critical #1; both new API clients (`TwitchEventSubChatClient`'s Helix calls, `YouTubeChatAPIClient`) send credentials via headers, never query params, directly avoiding a repeat of that review's Critical #2.
- **Out-of-scope items confirmed absent:** no LLM/AI scoring, no viewer-count signal combination, no candidate review/approval workflow, no Kick support — matches the spec's explicit exclusions.
- **Type/signature consistency checked:** `ChatPollResult`, `TwitchChatConnection`/`TwitchChatClient` protocol methods, `record_chat_activity(db, stream_session_id, bucket_start, message_count)`, `detect_clip_candidates_for_session(db, session, *, z_threshold, min_gap_seconds, lookback_minutes=10)`, and `get_valid_bot_access_token(db)`/`get_bot_user_id(db)` are each defined once and used with matching signatures everywhere they're consumed.
- **Known v1 simplifications, documented rather than silently omitted:** the Twitch WebSocket client doesn't seamlessly migrate on a `session_reconnect` message (relies on the outer connection dropping and the caller reconnecting fresh); no automatic recovery/re-enqueue of a chat listener after a worker restart (an open session with no active listener just accumulates a bucket gap, a detection-quality issue not a data-integrity one).
