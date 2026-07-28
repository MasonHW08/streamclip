from datetime import UTC, datetime

import pytest
from sqlalchemy import text

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


def _revoke_in_db(db, creator_id):
    """Revoke a creator at the DATABASE level, bypassing the ORM identity map.

    Mutating `creator.status` on the Python object would not reproduce the bug
    under test: the whole point is that the in-memory copy the poll loop holds
    is *stale* relative to the database. Issuing raw SQL is what a concurrent
    revocation by another process/request looks like from this session's point
    of view — the row changes underneath it while its identity map keeps the
    pre-revocation copy.
    """
    db.execute(
        text("UPDATE creators SET status = :status WHERE id = :creator_id"),
        {"status": CreatorStatus.REVOKED.value, "creator_id": creator_id},
    )


def _revoke_after_listing(monkeypatch, creator):
    """Make the poll task's creator listing revoke `creator` on its way out.

    `list_authorized_creators` is what loads every Creator row for the platform
    into the session's identity map; revoking immediately afterwards places the
    revocation exactly where it hurts — after the poll run has cached the
    creator as authorized, but before the loop's `is_authorized()` re-check.
    """
    from app.services.stream_discovery_service import (
        list_authorized_creators as real_list_authorized_creators,
    )

    def list_then_revoke(db, platform):
        creators = real_list_authorized_creators(db, platform=platform)
        _revoke_in_db(db, creator.id)
        return creators

    monkeypatch.setattr(
        "app.workers.stream_discovery_tasks.list_authorized_creators", list_then_revoke
    )


@pytest.mark.asyncio
async def test_poll_youtube_streams_sees_revocation_that_lands_mid_run(db_session, monkeypatch):
    """A revocation landing mid-run must stop that creator being monitored.

    `is_authorized()` uses `db.get(Creator, ...)`, which consults the session's
    identity map before the database. The poll loop's own
    `list_authorized_creators` call has already loaded every Creator for the
    platform into that identity map, so without an explicit `db.expire(creator)`
    the in-loop re-check reads the cached, pre-revocation object and never
    emits a SELECT — silently monitoring (and writing rows for) a creator who
    has revoked.
    """
    monkeypatch.setattr(
        "app.workers.stream_discovery_tasks.SessionLocal", lambda: _NonClosingSessionProxy(db_session)
    )
    creator = _authorized_creator(db_session, "youtube", "yt-revoked-mid-run")
    _revoke_after_listing(monkeypatch, creator)

    fake_client = FakeYouTubeClient()
    fake_client.stream_status["yt-revoked-mid-run"] = StreamInfo(
        external_stream_id="vid-revoked", title="t", category=None, viewer_count=5,
        started_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        "app.workers.stream_discovery_tasks.YouTubeAPIClient", lambda api_key: fake_client
    )

    await poll_youtube_streams({})

    assert (
        db_session.query(StreamSession).filter(StreamSession.creator_id == creator.id).count() == 0
    )


@pytest.mark.asyncio
async def test_poll_twitch_streams_backup_sees_revocation_that_lands_mid_run(
    db_session, monkeypatch
):
    """Same identity-map staleness hazard as the YouTube poller above."""
    monkeypatch.setattr(
        "app.workers.stream_discovery_tasks.SessionLocal", lambda: _NonClosingSessionProxy(db_session)
    )
    creator = _authorized_creator(db_session, "twitch", "tw-revoked-mid-run")
    _revoke_after_listing(monkeypatch, creator)

    fake_client = FakeTwitchClient()
    fake_client.stream_status["tw-revoked-mid-run"] = StreamInfo(
        external_stream_id="s-revoked", title="t", category="c", viewer_count=5,
        started_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        "app.workers.stream_discovery_tasks.TwitchAPIClient",
        lambda client_id, client_secret: fake_client,
    )

    await poll_twitch_streams_backup({})

    assert (
        db_session.query(StreamSession).filter(StreamSession.creator_id == creator.id).count() == 0
    )


@pytest.mark.asyncio
async def test_poll_youtube_streams_db_error_for_one_creator_does_not_poison_session_for_the_rest(
    db_session, monkeypatch
):
    """A DB-origin failure (not an HTTP-call failure) must not cascade.

    Unlike test_poll_youtube_streams_one_creator_failure_does_not_block_the_rest
    (where get_stream_status raises *before* any DB interaction),
    this simulates reconcile_creator_stream_state itself failing with a real
    Postgres-level error (e.g. a constraint violation or serialization
    failure) partway through its own work. That leaves the shared session's
    transaction aborted at the database level, not just a Python exception —
    so without an explicit db.rollback() in the poll loop's except block, the
    *next* creator's first query against that same session would itself raise
    (PendingRollbackError / "current transaction is aborted"), which the
    broad except would also swallow, making the healthy creator falsely look
    like it failed too.
    """
    monkeypatch.setattr(
        "app.workers.stream_discovery_tasks.SessionLocal", lambda: _NonClosingSessionProxy(db_session)
    )
    from app.services.stream_discovery_service import (
        reconcile_creator_stream_state as real_reconcile,
    )

    failing_creator = _authorized_creator(db_session, "youtube", "yt-db-fail")
    healthy_creator = _authorized_creator(db_session, "youtube", "yt-db-2")

    # list_authorized_creators's underlying query has no ORDER BY, so Postgres
    # does not guarantee it returns these two creators in insertion order —
    # pinning the iteration order explicitly is what makes this test
    # deterministic ("the failing creator runs first, then a later creator
    # must still succeed"), rather than depending on incidental row order.
    monkeypatch.setattr(
        "app.workers.stream_discovery_tasks.list_authorized_creators",
        lambda db, platform: [failing_creator, healthy_creator],
    )

    def flaky_reconcile(db, creator, stream_info):
        if creator.platform_channel_id == "yt-db-fail":
            # Real DB-level error (undefined table), not a bare Python
            # exception — this is what actually leaves a Postgres
            # transaction/savepoint aborted, unlike raising before touching
            # the session at all.
            db.execute(text("SELECT * FROM this_table_does_not_exist_xyz"))
        else:
            real_reconcile(db, creator, stream_info)

    monkeypatch.setattr(
        "app.workers.stream_discovery_tasks.reconcile_creator_stream_state", flaky_reconcile
    )

    fake_client = FakeYouTubeClient()
    fake_client.stream_status["yt-db-fail"] = StreamInfo(
        external_stream_id="vid-fail", title="t", category=None, viewer_count=1, started_at=datetime.now(UTC)
    )
    fake_client.stream_status["yt-db-2"] = StreamInfo(
        external_stream_id="vid-2", title="t", category=None, viewer_count=5, started_at=datetime.now(UTC)
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
