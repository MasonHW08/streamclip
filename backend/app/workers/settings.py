from arq.connections import RedisSettings

from app.core.config import get_settings
from app.workers.tasks import send_approved_outreach_email


class WorkerSettings:
    functions = [send_approved_outreach_email]  # noqa: RUF012 (arq's own idiom, not a mutable-default trap)
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
