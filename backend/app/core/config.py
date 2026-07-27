from functools import lru_cache
from typing import Self

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER_JWT_SECRET = "change-me-to-a-long-random-string"
MIN_JWT_SECRET_LENGTH = 32
MIN_TWITCH_WEBHOOK_SECRET_LENGTH = 16


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
    twitch_eventsub_callback_url: str = ""
    youtube_api_key: str = ""

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

    @model_validator(mode="after")
    def _reject_insecure_twitch_webhook_secret(self) -> Self:
        """Fail fast outside development if TWITCH_WEBHOOK_SECRET is empty or too short.

        HMAC-SHA256 over an empty (or guessable, too-short) key is still a
        well-defined, computable function: anyone who knows the secret is blank —
        and blank is the shipped default — can compute a valid `sha256=` signature
        for an arbitrary forged EventSub payload. Since signature verification is
        the entire authentication mechanism for the Twitch webhook route, a blank
        secret in production makes that check a no-op, letting an unauthenticated
        caller inject fake stream.online/stream.offline events. Development is
        exempt so local/test runs can omit it entirely.
        """
        if self.environment == "development":
            return self
        if len(self.twitch_webhook_secret) < MIN_TWITCH_WEBHOOK_SECRET_LENGTH:
            raise ValueError(
                "TWITCH_WEBHOOK_SECRET is missing or too short "
                f"({len(self.twitch_webhook_secret)} characters); at least "
                f"{MIN_TWITCH_WEBHOOK_SECRET_LENGTH} are required when "
                f"ENVIRONMENT={self.environment!r}."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
