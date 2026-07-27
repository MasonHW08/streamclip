from datetime import date

from fastapi import FastAPI
from fastapi.testclient import TestClient
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.public import router
from app.core.db import get_db
from app.core.rate_limit import limiter
from app.core.security import create_magic_link_token
from app.models.agreement import AgreementTermsVersion
from app.models.creator import Creator, CreatorStatus
from app.services.agreement_service import accept_agreement


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
