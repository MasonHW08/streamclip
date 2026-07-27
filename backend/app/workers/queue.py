from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.config import get_settings

_pool: ArqRedis | None = None


async def _get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        settings = get_settings()
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _pool


async def enqueue_send_outreach_email(outreach_email_id: int) -> None:
    pool = await _get_pool()
    await pool.enqueue_job("send_approved_outreach_email", outreach_email_id)
