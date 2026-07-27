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
    issued_at = datetime.now(UTC)
    payload = {
        "sub": str(creator_id),
        "purpose": purpose,
        # Sub-second float rather than PyJWT's whole-second datetime encoding (RFC 7519
        # NumericDate permits both). `iat` is compared against `agreements.revoked_at`
        # to reject links minted before a revocation; at whole-second resolution a link
        # minted moments *after* a revoke can floor to before it and be wrongly refused.
        "iat": issued_at.timestamp(),
        "exp": issued_at + timedelta(days=days),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def verify_magic_link_token_claims(
    token: str, expected_purpose: MagicLinkPurpose
) -> tuple[int, datetime | None]:
    """Verify a magic-link token and return ``(creator_id, issued_at)``.

    ``issued_at`` is ``None`` for tokens minted before the ``iat`` claim existed;
    callers must treat that as "unknown age" rather than "brand new".
    """
    settings = get_settings()
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        if payload.get("purpose") != expected_purpose:
            raise InvalidMagicLinkToken("This link isn't valid for this action.")
        creator_id = int(payload["sub"])
        raw_iat = payload.get("iat")
        issued_at = datetime.fromtimestamp(float(raw_iat), UTC) if raw_iat is not None else None
        return creator_id, issued_at
    except jwt.ExpiredSignatureError as exc:
        raise InvalidMagicLinkToken("This link has expired. Request a new one.") from exc
    except jwt.InvalidTokenError as exc:
        raise InvalidMagicLinkToken("This link isn't valid.") from exc
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidMagicLinkToken("This link isn't valid.") from exc


def verify_magic_link_token(token: str, expected_purpose: MagicLinkPurpose) -> int:
    creator_id, _issued_at = verify_magic_link_token_claims(token, expected_purpose)
    return creator_id
