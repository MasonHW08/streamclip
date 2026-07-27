import re
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.public import router
from app.core.db import get_db
from app.core.rate_limit import limiter
from app.core.security import create_magic_link_token, verify_magic_link_token
from app.models.agreement import AgreementTermsVersion
from app.models.creator import Creator, CreatorStatus
from app.services.agreement_service import accept_agreement


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The limiter is a module-level singleton shared by every test in this file.

    Without this, tests silently start tripping the 10/minute cap on /partner/*
    once enough of them run, and the failure looks like a broken endpoint.
    """
    limiter.reset()
    yield
    limiter.reset()


def _make_app(db_session):
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    return app


def _seed_terms(db_session):
    terms = AgreementTermsVersion(
        version="v1", effective_date=date.today(), body_markdown="Terms at {rev_share_pct}%"
    )
    db_session.add(terms)
    db_session.commit()
    return terms


def test_agree_page_rejects_invalid_token(db_session):
    app = _make_app(db_session)
    client = TestClient(app)
    response = client.get("/partner/agree", params={"token": "garbage"})
    assert response.status_code == 400


def test_agree_flow_authorizes_creator(db_session):
    _seed_terms(db_session)
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    token = create_magic_link_token(creator.id, "agree")

    app = _make_app(db_session)
    client = TestClient(app)

    get_response = client.get("/partner/agree", params={"token": token})
    assert get_response.status_code == 200
    assert "A" in get_response.text

    post_response = client.post("/partner/agree", data={"token": token, "signature_name": "A Streamer"})
    assert post_response.status_code == 200

    db_session.refresh(creator)
    assert creator.status == CreatorStatus.AUTHORIZED


def test_revoke_flow_revokes_creator(db_session):
    _seed_terms(db_session)
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    accept_agreement(db_session, creator.id, signature_name="A", ip="1.2.3.4", user_agent="pytest")
    revoke_token = create_magic_link_token(creator.id, "revoke")

    app = _make_app(db_session)
    client = TestClient(app)

    response = client.post("/partner/revoke", data={"token": revoke_token})
    assert response.status_code == 200

    db_session.refresh(creator)
    assert creator.status == CreatorStatus.REVOKED


def test_agree_token_rejected_on_revoke_endpoint(db_session):
    _seed_terms(db_session)
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    agree_token = create_magic_link_token(creator.id, "agree")

    app = _make_app(db_session)
    client = TestClient(app)

    response = client.get("/partner/revoke", params={"token": agree_token})
    assert response.status_code == 400

    db_session.refresh(creator)
    assert creator.status == CreatorStatus.PROSPECT


def test_revoke_token_rejected_on_agree_endpoint(db_session):
    _seed_terms(db_session)
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    revoke_token = create_magic_link_token(creator.id, "revoke")

    app = _make_app(db_session)
    client = TestClient(app)

    response = client.get("/partner/agree", params={"token": revoke_token})
    assert response.status_code == 400

    db_session.refresh(creator)
    assert creator.status == CreatorStatus.PROSPECT


def test_double_submit_agree_returns_clean_conflict_not_crash(db_session):
    _seed_terms(db_session)
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    token = create_magic_link_token(creator.id, "agree")

    app = _make_app(db_session)
    client = TestClient(app)

    first = client.post("/partner/agree", data={"token": token, "signature_name": "A"})
    assert first.status_code == 200

    second = client.post("/partner/agree", data={"token": token, "signature_name": "A"})
    assert second.status_code == 409

    db_session.refresh(creator)
    assert creator.status == CreatorStatus.AUTHORIZED


def test_agree_token_for_missing_creator_returns_clean_error_not_crash(db_session):
    _seed_terms(db_session)
    token = create_magic_link_token(creator_id=999999, purpose="agree")

    app = _make_app(db_session)
    client = TestClient(app)

    response = client.get("/partner/agree", params={"token": token})
    assert response.status_code == 400


def _revoke_token_from_page(html):
    """Pull the revoke token out of a rendered page's revoke link."""
    match = re.search(r'href="[^"]*?/partner/revoke\?token=([\w.\-]+)"', html)
    assert match, f"no revoke link found in page: {html}"
    return match.group(1)


def test_agreement_page_offers_a_working_revoke_link(db_session):
    _seed_terms(db_session)
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    token = create_magic_link_token(creator.id, "agree")

    app = _make_app(db_session)
    client = TestClient(app)

    page = client.get("/partner/agree", params={"token": token})
    assert page.status_code == 200
    revoke_token = _revoke_token_from_page(page.text)
    assert verify_magic_link_token(revoke_token, "revoke") == creator.id


def test_confirmation_page_after_accept_offers_a_working_revoke_link(db_session):
    _seed_terms(db_session)
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    token = create_magic_link_token(creator.id, "agree")

    app = _make_app(db_session)
    client = TestClient(app)

    confirmation = client.post(
        "/partner/agree", data={"token": token, "signature_name": "A Streamer"}
    )
    assert confirmation.status_code == 200
    revoke_token = _revoke_token_from_page(confirmation.text)
    assert verify_magic_link_token(revoke_token, "revoke") == creator.id

    # And it actually works end to end.
    revoked = client.post("/partner/revoke", data={"token": revoke_token})
    assert revoked.status_code == 200
    db_session.refresh(creator)
    assert creator.status == CreatorStatus.REVOKED


def test_stale_agree_token_cannot_silently_undo_a_revoke(db_session):
    _seed_terms(db_session)
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()
    stale_token = create_magic_link_token(creator.id, "agree")

    app = _make_app(db_session)
    client = TestClient(app)

    confirmation = client.post(
        "/partner/agree", data={"token": stale_token, "signature_name": "A"}
    )
    assert confirmation.status_code == 200
    revoke_token = _revoke_token_from_page(confirmation.text)
    client.post("/partner/revoke", data={"token": revoke_token})

    # Replaying the original agree email must not silently re-authorize.
    replay = client.post("/partner/agree", data={"token": stale_token, "signature_name": "A"})
    assert replay.status_code == 409
    assert "no longer valid" in replay.text

    db_session.refresh(creator)
    assert creator.status == CreatorStatus.REVOKED


def test_fresh_agree_token_after_revoke_reauthorizes(db_session):
    _seed_terms(db_session)
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add(creator)
    db_session.commit()

    app = _make_app(db_session)
    client = TestClient(app)

    confirmation = client.post(
        "/partner/agree",
        data={"token": create_magic_link_token(creator.id, "agree"), "signature_name": "A"},
    )
    revoke_token = _revoke_token_from_page(confirmation.text)
    client.post("/partner/revoke", data={"token": revoke_token})

    # A newly minted link (e.g. a follow-up outreach email) must still work.
    fresh_token = create_magic_link_token(creator.id, "agree")
    reaccepted = client.post("/partner/agree", data={"token": fresh_token, "signature_name": "A"})
    assert reaccepted.status_code == 200

    db_session.refresh(creator)
    assert creator.status == CreatorStatus.AUTHORIZED
