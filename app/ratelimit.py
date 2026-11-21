from __future__ import annotations
import asyncio

from app.config import settings

_redis = None


def set_redis(r) -> None:
    global _redis
    _redis = r


async def check_rate_limit(client_ip: str) -> tuple[bool, int]:
    """
    Sliding-window token bucket via Redis INCR + EXPIRE.
    Returns (allowed, retry_after_seconds).
    Fails open if Redis is unavailable.
    """
    if _redis is None:
        return True, 0

    key = f"rl:{client_ip}"
    window = settings.rate_limit_window
    limit = settings.rate_limit_creates

    try:
        pipe = _redis.pipeline(transaction=False)
        pipe.incr(key)
        pipe.ttl(key)
        results = await asyncio.wait_for(pipe.execute(), timeout=settings.redis_timeout)
        count, ttl = int(results[0]), int(results[1])

        # Set expiry on first use or if it was lost
        if ttl < 0:
            await asyncio.wait_for(
                _redis.expire(key, window), timeout=settings.redis_timeout
            )
            ttl = window

        if count > limit:
            return False, ttl
        return True, 0
    except Exception:
        return True, 0  # fail open — never block creates because Redis is down
