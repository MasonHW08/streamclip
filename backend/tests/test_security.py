from datetime import timedelta

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import (
    InvalidMagicLinkToken,
    create_magic_link_token,
    verify_magic_link_token,
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
