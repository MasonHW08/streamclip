import pytest
from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials

from app.api.internal_auth import require_internal_user
from app.models.user import User
from app.services.password import hash_password


def test_valid_credentials_returns_user(db_session):
    user = User(email="team@streamclip.co", hashed_password=hash_password("secret"), role="admin")
    db_session.add(user)
    db_session.commit()

    result = require_internal_user(
        credentials=HTTPBasicCredentials(username="team@streamclip.co", password="secret"),
        db=db_session,
    )
    assert result.id == user.id


def test_invalid_password_raises_401(db_session):
    user = User(email="team@streamclip.co", hashed_password=hash_password("secret"), role="admin")
    db_session.add(user)
    db_session.commit()

    with pytest.raises(HTTPException) as exc_info:
        require_internal_user(
            credentials=HTTPBasicCredentials(username="team@streamclip.co", password="wrong"),
            db=db_session,
        )
    assert exc_info.value.status_code == 401


def test_unknown_user_raises_401(db_session):
    with pytest.raises(HTTPException) as exc_info:
        require_internal_user(
            credentials=HTTPBasicCredentials(username="nobody@streamclip.co", password="secret"),
            db=db_session,
        )
    assert exc_info.value.status_code == 401
