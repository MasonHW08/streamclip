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


def test_webhooks_router_is_registered():
    """Verify the webhooks router is included in the main app."""
    client = TestClient(app)
    # A POST to /webhooks/twitch/eventsub should not return 404 (will fail sig check instead)
    response = client.post("/webhooks/twitch/eventsub", json={})
    # Should get 403 (signature verification failure) not 404 (route not found)
    assert response.status_code == 403
