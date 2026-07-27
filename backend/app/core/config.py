from functools import lru_cache
from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_JWT_SECRET = "change-me-to-a-long-random-string"
MIN_JWT_SECRET_LENGTH = 32


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
    twitch_webhook_secret: str = ""
    twitch_client_id: str = ""
    twitch_client_secret: str = ""

    @model_validator(mode="after")
    def _reject_insecure_jwt_secret(self) -> Self:
        """Fail fast outside development if JWT_SECRET is the placeholder or too short.

        A forgeable signing key means anyone can mint a valid magic link for any
        creator_id, i.e. forge consent. Development is exempt so local/test runs can
        use a short throwaway secret.
        """
        if self.environment == "development":
            return self
        if self.jwt_secret == PLACEHOLDER_JWT_SECRET:
            raise ValueError(
                "JWT_SECRET is still the placeholder from .env.example. Set a real "
                f"random secret (at least {MIN_JWT_SECRET_LENGTH} characters) before "
                f"running with ENVIRONMENT={self.environment!r}."
            )
        if len(self.jwt_secret) < MIN_JWT_SECRET_LENGTH:
            raise ValueError(
                f"JWT_SECRET is too short ({len(self.jwt_secret)} characters); at least "
                f"{MIN_JWT_SECRET_LENGTH} are required when "
                f"ENVIRONMENT={self.environment!r}."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
