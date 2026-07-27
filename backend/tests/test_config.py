from app.core.config import Settings


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("JWT_SECRET", "test-secret")
    settings = Settings()
    assert settings.database_url == "postgresql+psycopg://u:p@localhost/db"
    assert settings.jwt_secret == "test-secret"
    assert settings.environment == "development"
    assert settings.default_rev_share_pct == 50.0
