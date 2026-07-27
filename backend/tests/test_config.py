import pytest

from app.core.config import (
    MIN_JWT_SECRET_LENGTH,
    MIN_TWITCH_WEBHOOK_SECRET_LENGTH,
    PLACEHOLDER_JWT_SECRET,
    Settings,
)


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    settings = Settings()
    assert settings.database_url == "postgresql+psycopg://u:p@localhost/db"
    assert settings.jwt_secret == "test-secret"
    assert settings.environment == "development"
    assert settings.default_rev_share_pct == 50.0


def _prod_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("ENVIRONMENT", "production")


def test_placeholder_jwt_secret_rejected_outside_development(monkeypatch):
    _prod_env(monkeypatch)
    monkeypatch.setenv("JWT_SECRET", PLACEHOLDER_JWT_SECRET)
    with pytest.raises(ValueError, match="placeholder"):
        Settings()


def test_short_jwt_secret_rejected_outside_development(monkeypatch):
    _prod_env(monkeypatch)
    monkeypatch.setenv("JWT_SECRET", "a" * (MIN_JWT_SECRET_LENGTH - 1))
    with pytest.raises(ValueError, match="too short"):
        Settings()


def test_placeholder_jwt_secret_allowed_in_development(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("JWT_SECRET", PLACEHOLDER_JWT_SECRET)
    assert Settings().jwt_secret == PLACEHOLDER_JWT_SECRET


def test_short_jwt_secret_allowed_in_development(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    assert Settings().jwt_secret == "test-secret"


def test_real_jwt_secret_accepted_in_production(monkeypatch):
    _prod_env(monkeypatch)
    real_secret = "z" * MIN_JWT_SECRET_LENGTH
    monkeypatch.setenv("JWT_SECRET", real_secret)
    monkeypatch.setenv("TWITCH_WEBHOOK_SECRET", "z" * MIN_TWITCH_WEBHOOK_SECRET_LENGTH)
    settings = Settings()
    assert settings.jwt_secret == real_secret
    assert settings.environment == "production"


def _prod_env_with_valid_jwt(monkeypatch):
    _prod_env(monkeypatch)
    monkeypatch.setenv("JWT_SECRET", "z" * MIN_JWT_SECRET_LENGTH)


def test_empty_twitch_webhook_secret_rejected_outside_development(monkeypatch):
    _prod_env_with_valid_jwt(monkeypatch)
    monkeypatch.setenv("TWITCH_WEBHOOK_SECRET", "")
    with pytest.raises(ValueError, match="TWITCH_WEBHOOK_SECRET"):
        Settings()


def test_missing_twitch_webhook_secret_rejected_outside_development(monkeypatch):
    _prod_env_with_valid_jwt(monkeypatch)
    with pytest.raises(ValueError, match="TWITCH_WEBHOOK_SECRET"):
        Settings()


def test_short_twitch_webhook_secret_rejected_outside_development(monkeypatch):
    _prod_env_with_valid_jwt(monkeypatch)
    monkeypatch.setenv("TWITCH_WEBHOOK_SECRET", "a" * (MIN_TWITCH_WEBHOOK_SECRET_LENGTH - 1))
    with pytest.raises(ValueError, match="too short"):
        Settings()


def test_twitch_webhook_secret_allowed_empty_in_development(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    assert Settings().twitch_webhook_secret == ""


def test_real_twitch_webhook_secret_accepted_in_production(monkeypatch):
    _prod_env_with_valid_jwt(monkeypatch)
    real_secret = "z" * MIN_TWITCH_WEBHOOK_SECRET_LENGTH
    monkeypatch.setenv("TWITCH_WEBHOOK_SECRET", real_secret)
    settings = Settings()
    assert settings.twitch_webhook_secret == real_secret
