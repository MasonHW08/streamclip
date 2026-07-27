import pytest

from app.core.config import (
    MIN_JWT_SECRET_LENGTH,
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
    settings = Settings()
    assert settings.jwt_secret == real_secret
    assert settings.environment == "production"
