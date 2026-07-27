from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.internal import router
from app.core.db import get_db
from app.models.creator import Creator
from app.models.user import User
from app.services.outreach_service import draft_outreach_email
from app.services.password import hash_password


def _make_app(db_session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    return app


def test_list_outreach_requires_auth(db_session):
    app = _make_app(db_session)
    client = TestClient(app)
    response = client.get("/internal/outreach")
    assert response.status_code == 401


def test_list_and_approve_outreach(db_session, monkeypatch):
    monkeypatch.setattr("app.api.internal.enqueue_send_outreach_email", AsyncMock())

    user = User(email="team@streamclip.co", hashed_password=hash_password("secret"), role="admin")
    creator = Creator(platform="twitch", platform_channel_id="1", display_name="A")
    db_session.add_all([user, creator])
    db_session.commit()
    draft = draft_outreach_email(db_session, creator.id)

    app = _make_app(db_session)
    client = TestClient(app)
    auth = ("team@streamclip.co", "secret")

    list_response = client.get("/internal/outreach", auth=auth)
    assert list_response.status_code == 200
    assert [d["id"] for d in list_response.json()] == [draft.id]

    approve_response = client.post(f"/internal/outreach/{draft.id}/approve", auth=auth)
    assert approve_response.status_code == 200
    assert approve_response.json()["status"] == "approved"
