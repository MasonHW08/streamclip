from fastapi.testclient import TestClient

from app.main import app


def test_unauthenticated_internal_call_rejected():
    client = TestClient(app)
    response = client.get("/internal/outreach")
    assert response.status_code == 401


def test_invalid_agree_token_rejected():
    client = TestClient(app)
    response = client.get("/partner/agree", params={"token": "garbage"})
    assert response.status_code == 400
