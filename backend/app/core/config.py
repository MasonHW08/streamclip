from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    redis_url: str = "redis://localhost:6379/0"
    resend_api_key: str = ""
    resend_from_address: str = "partners@streamclip.co"
    jwt_secret: str
    magic_link_expiry_days: int = 14
    public_base_url: str = "http://localhost:8000"
    default_rev_share_pct: float = 50.0
    environment: str = "development"


@lru_cache
def get_settings() -> Settings:
    return Settings()
