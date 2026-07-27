from datetime import UTC, datetime, timedelta
from typing import Literal

import jwt

from app.core.config import get_settings

MagicLinkPurpose = Literal["agree", "revoke"]


class InvalidMagicLinkToken(Exception):
    pass


def create_magic_link_token(
    creator_id: int, purpose: MagicLinkPurpose, expires_in_days: int | None = None
) -> str:
    settings = get_settings()
    days = expires_in_days if expires_in_days is not None else settings.magic_link_expiry_days
    payload = {
        "sub": str(creator_id),
        "purpose": purpose,
        "exp": datetime.now(UTC) + timedelta(days=days),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def verify_magic_link_token(token: str, expected_purpose: MagicLinkPurpose) -> int:
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise InvalidMagicLinkToken("This link has expired. Request a new one.") from exc
    except jwt.InvalidTokenError as exc:
        raise InvalidMagicLinkToken("This link isn't valid.") from exc
    if payload.get("purpose") != expected_purpose:
        raise InvalidMagicLinkToken("This link isn't valid for this action.")
    return int(payload["sub"])
