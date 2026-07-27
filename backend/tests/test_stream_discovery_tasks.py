from datetime import UTC, datetime

import pytest

from app.models.agreement import Agreement, AgreementStatus, AgreementTermsVersion
from app.models.creator import Creator, CreatorStatus
from app.models.stream_session import StreamSession
from app.services.stream_info import StreamInfo
from app.services.twitch_client import FakeTwitchClient
from app.services.youtube_client import FakeYouTubeClient
from app.workers.stream_discovery_tasks import poll_twitch_streams_backup, poll_youtube_streams


class _NonClosingSessionProxy:
    """Proxies to a shared test session but no-ops `close()`.

    The worker's `finally: db.close()` is correct for production, where
    `SessionLocal()` opens a fresh session per job. In tests we monkeypatch
    `SessionLocal` to hand back the shared, transactional `db_session`
    fixture instead — a real `close()` on that session would expunge its
    objects and break the fixture for the rest of the test (and its own
    teardown). This wrapper lets the worker's real commit/get/query code
    path run unmodified while leaving fixture lifecycle to the fixture.

    Same pattern as `tests/test_worker_tasks.py`.
    """

    def __init__(self, session):
        self._session = session

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._session, name)


def _authorized_creator(db_session, platform, channel_id):
    terms = AgreementTermsVersion(
        version=f"v1-{platform}-{channel_id}", effective_date=datetime.now(UTC).date(), body_markdown="x"
    )
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
    monkeypatch.setattr(
        "app.workers.stream_discovery_tasks.SessionLocal", lambda: _NonClosingSessionProxy(db_session)
    )
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
    monkeypatch.setattr(
        "app.workers.stream_discovery_tasks.SessionLocal", lambda: _NonClosingSessionProxy(db_session)
    )
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


class _FlakyYouTubeClient:
    """Raises for one configured channel_id, behaves like FakeYouTubeClient for others."""

    def __init__(self, stream_status, failing_channel_id):
        self.stream_status = stream_status
        self.failing_channel_id = failing_channel_id

    def get_stream_status(self, channel_id):
        if channel_id == self.failing_channel_id:
            raise RuntimeError("simulated transient API failure")
        return self.stream_status.get(channel_id)


@pytest.mark.asyncio
async def test_poll_youtube_streams_one_creator_failure_does_not_block_the_rest(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.workers.stream_discovery_tasks.SessionLocal", lambda: _NonClosingSessionProxy(db_session)
    )
    failing_creator = _authorized_creator(db_session, "youtube", "yt-fail")
    healthy_creator = _authorized_creator(db_session, "youtube", "yt-2")

    fake_client = _FlakyYouTubeClient(
        stream_status={
            "yt-2": StreamInfo(
                external_stream_id="vid-2", title="t", category=None, viewer_count=5, started_at=datetime.now(UTC)
            )
        },
        failing_channel_id="yt-fail",
    )
    monkeypatch.setattr(
        "app.workers.stream_discovery_tasks.YouTubeAPIClient", lambda api_key: fake_client
    )

    await poll_youtube_streams({})

    failing_session = (
        db_session.query(StreamSession).filter(StreamSession.creator_id == failing_creator.id).first()
    )
    healthy_session = (
        db_session.query(StreamSession).filter(StreamSession.creator_id == healthy_creator.id).first()
    )
    assert failing_session is None
    assert healthy_session is not None
