from __future__ import annotations
import asyncio
import time
from typing import Optional, Callable, Awaitable

import orjson
from cachetools import TTLCache

from app.config import settings
from app.metrics import CACHE_HIT, CACHE_MISS

# Pre-bind Prometheus counter labels to eliminate dictionary lookups on hot path
CACHE_HIT_LOCAL = CACHE_HIT.labels(layer="local")
CACHE_MISS_LOCAL = CACHE_MISS.labels(layer="local")
CACHE_HIT_REDIS = CACHE_HIT.labels(layer="redis")
CACHE_MISS_REDIS = CACHE_MISS.labels(layer="redis")
CACHE_HIT_NEG = CACHE_HIT.labels(layer="neg")

# ── In-process L1 cache ───────────────────────────────────────────────────────
# In asyncio, simple dict/TTLCache reads are atomic within the single-threaded event loop
_local: TTLCache = TTLCache(
    maxsize=settings.local_cache_max, ttl=settings.local_cache_ttl
)
_neg: TTLCache = TTLCache(
    maxsize=settings.negative_cache_max, ttl=settings.negative_cache_ttl
)
_local_lock = asyncio.Lock()

# Single-flight: maps code -> (Event, result) so concurrent waiters share one DB hit
_inflight: dict[str, asyncio.Event] = {}
_inflight_results: dict[str, Optional[str]] = {}

# Redis client — set by lifespan via set_redis()
_redis = None


def set_redis(r) -> None:
    global _redis
    _redis = r


def _effective_ttl(url_data: dict) -> int:
    """Return Redis TTL capped by remaining link lifetime."""
    expires_at = url_data.get("expires_at")
    if expires_at is None:
        return settings.cache_ttl_seconds
    remaining = int(expires_at - time.time())
    if remaining <= 0:
        return 1  # expire immediately
    return min(remaining, settings.cache_ttl_seconds)


# ── Public read API ───────────────────────────────────────────────────────────

async def get_url(code: str) -> Optional[str]:
    """
    Look up long_url for code.
    Layer order: L1 (local TTLCache) -> negative cache -> L2 (Redis).
    Hot path: zero locks on L1 read for sub-millisecond redirect latency.
    """
    # 1. L1 local cache — synchronous check, no async lock
    val = _local.get(code)
    if val is not None:
        CACHE_HIT_LOCAL.inc()
        return val
    CACHE_MISS_LOCAL.inc()

    # 2. Negative cache (locally known-missing)
    if code in _neg:
        CACHE_HIT_NEG.inc()
        return None

    # 3. L2 Redis
    if _redis is not None:
        try:
            raw = await asyncio.wait_for(
                _redis.get(f"url:{code}"), timeout=settings.redis_timeout
            )
            if raw is not None:
                data = orjson.loads(raw)
                long_url = data.get("long_url")
                if long_url:
                    # check expiry encoded in the value
                    expires_at = data.get("expires_at")
                    if expires_at and time.time() > expires_at:
                        CACHE_MISS_REDIS.inc()
                        return None
                    CACHE_HIT_REDIS.inc()
                    # Warm L1 cache
                    _local[code] = long_url
                    return long_url
                # Empty dict stored as negative
                CACHE_HIT_NEG.inc()
                return None
            CACHE_MISS_REDIS.inc()
        except Exception:
            # Redis down — fall through to DB
            CACHE_MISS_REDIS.inc()

    return None  # caller performs DB lookup


# ── Public write API ──────────────────────────────────────────────────────────

async def set_url(
    code: str, long_url: str, expires_at: Optional[float] = None
) -> None:
    """Write code -> long_url into L1 and Redis."""
    _local[code] = long_url

    if _redis is not None:
        data: dict = {"long_url": long_url}
        if expires_at is not None:
            data["expires_at"] = expires_at
        ttl = _effective_ttl(data)
        try:
            await asyncio.wait_for(
                _redis.set(f"url:{code}", orjson.dumps(data), ex=ttl),
                timeout=settings.redis_timeout,
            )
        except Exception:
            pass  # Redis down — not fatal, DB is source of truth


async def set_negative(code: str) -> None:
    """Mark code as known-missing to blunt scanners."""
    _neg[code] = True
    if _redis is not None:
        try:
            await asyncio.wait_for(
                _redis.set(
                    f"url:{code}",
                    orjson.dumps({}),
                    ex=settings.negative_cache_ttl,
                ),
                timeout=settings.redis_timeout,
            )
        except Exception:
            pass


async def delete_url(code: str) -> None:
    """Remove a code from all cache layers (called on expiry purge)."""
    _local.pop(code, None)
    _neg.pop(code, None)
    if _redis is not None:
        try:
            await asyncio.wait_for(
                _redis.delete(f"url:{code}"), timeout=settings.redis_timeout
            )
        except Exception:
            pass


# ── Single-flight coalescing ──────────────────────────────────────────────────

async def single_flight_lookup(
    code: str,
    fetch_fn: Callable[[], Awaitable[Optional[str]]],
) -> Optional[str]:
    """
    If multiple coroutines race to look up the same cold code, only the first
    one actually calls fetch_fn; the others wait and get the same result.
    fetch_fn must return Optional[str] (the long_url or None).
    """
    if code in _inflight:
        event = _inflight[code]
        await event.wait()
        return _inflight_results.get(code)

    event = asyncio.Event()
    _inflight[code] = event
    try:
        result = await fetch_fn()
        _inflight_results[code] = result
        return result
    finally:
        event.set()
        _inflight.pop(code, None)
        _inflight_results.pop(code, None)


# ── Health check ──────────────────────────────────────────────────────────────

async def check_redis() -> bool:
    if _redis is None:
        return False
    try:
        return bool(await asyncio.wait_for(_redis.ping(), timeout=1.0))
    except Exception:
        return False
