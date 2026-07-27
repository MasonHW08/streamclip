from datetime import UTC, datetime

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import (
    InvalidMagicLinkToken,
    create_magic_link_token,
    verify_magic_link_token,
    verify_magic_link_token_claims,
)


def test_roundtrip():
    token = create_magic_link_token(42, "agree")
    assert verify_magic_link_token(token, "agree") == 42


def test_wrong_purpose_rejected():
    token = create_magic_link_token(42, "agree")
    with pytest.raises(InvalidMagicLinkToken):
        verify_magic_link_token(token, "revoke")


def test_expired_token_rejected():
    token = create_magic_link_token(42, "agree", expires_in_days=-1)
    with pytest.raises(InvalidMagicLinkToken):
        verify_magic_link_token(token, "agree")


def test_tampered_token_rejected():
    token = create_magic_link_token(42, "agree")
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    with pytest.raises(InvalidMagicLinkToken):
        verify_magic_link_token(tampered, "agree")


def test_wrong_secret_rejected():
    settings = get_settings()
    forged = jwt.encode({"sub": "42", "purpose": "agree"}, "wrong-secret", algorithm="HS256")
    with pytest.raises(InvalidMagicLinkToken):
        verify_magic_link_token(forged, "agree")
    assert settings.jwt_secret != "wrong-secret"


def test_missing_sub_claim_rejected():
    settings = get_settings()
    forged = jwt.encode({"purpose": "agree"}, settings.jwt_secret, algorithm="HS256")
    with pytest.raises(InvalidMagicLinkToken):
        verify_magic_link_token(forged, "agree")


def test_non_numeric_sub_rejected():
    settings = get_settings()
    forged = jwt.encode({"sub": "not-a-number", "purpose": "agree"}, settings.jwt_secret, algorithm="HS256")
    with pytest.raises(InvalidMagicLinkToken):
        verify_magic_link_token(forged, "agree")


def test_token_carries_issued_at_claim():
    before = datetime.now(UTC)
    token = create_magic_link_token(42, "agree")
    after = datetime.now(UTC)

    creator_id, issued_at = verify_magic_link_token_claims(token, "agree")

    assert creator_id == 42
    assert issued_at is not None
    # Encoded with sub-second precision, so this brackets exactly.
    assert before <= issued_at <= after


def test_claims_helper_rejects_wrong_purpose():
    token = create_magic_link_token(42, "agree")
    with pytest.raises(InvalidMagicLinkToken):
        verify_magic_link_token_claims(token, "revoke")


def test_legacy_token_without_iat_reports_unknown_issue_time():
    settings = get_settings()
    legacy = jwt.encode(
        {"sub": "42", "purpose": "agree"}, settings.jwt_secret, algorithm="HS256"
    )
    creator_id, issued_at = verify_magic_link_token_claims(legacy, "agree")
    assert creator_id == 42
    assert issued_at is None
