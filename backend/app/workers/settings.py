from arq.connections import RedisSettings
from arq.cron import cron

from app.core.config import get_settings
from app.workers.stream_discovery_tasks import (
    poll_twitch_streams_backup,
    poll_youtube_streams,
    reconcile_twitch_subscriptions_task,
)
from app.workers.tasks import send_approved_outreach_email


class WorkerSettings:
    functions = [send_approved_outreach_email]  # noqa: RUF012 (arq's own idiom, not a mutable-default trap)
    cron_jobs = [  # noqa: RUF012 (arq's own idiom, not a mutable-default trap)
        cron(poll_youtube_streams, minute=set(range(0, 60, 5))),
        cron(poll_twitch_streams_backup, minute=set(range(0, 60, 15))),
        cron(reconcile_twitch_subscriptions_task, minute=set(range(0, 60, 30))),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
