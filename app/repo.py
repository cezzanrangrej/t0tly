from __future__ import annotations
import time
from typing import Optional

import asyncpg

from app.config import settings
from app.metrics import CACHE_MISS, DB_POOL_SIZE

_pool: Optional[asyncpg.Pool] = None


def set_pool(pool: asyncpg.Pool) -> None:
    global _pool
    _pool = pool
    DB_POOL_SIZE.set(settings.db_max_pool)


async def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized")
    return _pool


async def get_url_with_expiry(code: str) -> tuple[Optional[str], Optional[float]]:
    """
    Fetch (long_url, expires_at_timestamp) from DB.
    Returns (None, None) if not found or already expired.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT long_url, expires_at FROM urls WHERE code = $1",
            code,
        )
    CACHE_MISS.labels(layer="db").inc()
    if row is None:
        return None, None
    expires_at = row["expires_at"]
    ts = expires_at.timestamp() if expires_at else None
    if ts is not None and ts < time.time():
        return None, None
    return row["long_url"], ts


async def insert_url(
    code: str, long_url: str, expires_at=None
) -> bool:
    """
    Insert a new URL mapping.
    Returns True on success, False on PK conflict (alias/code collision).
    Relies solely on the PK constraint — no check-then-insert.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO urls (code, long_url, expires_at) VALUES ($1, $2, $3)",
                code,
                long_url,
                expires_at,
            )
            return True
        except asyncpg.UniqueViolationError:
            return False


async def url_exists(code: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT 1 FROM urls WHERE code = $1", code
        )
    return row is not None


async def get_click_count(code: str) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM clicks WHERE code = $1", code
        )
    return int(row["n"]) if row else 0


async def insert_clicks_batch(events: list[dict]) -> None:
    """
    Bulk-insert analytics click rows.
    In 'new' build: streaming binary COPY protocol inside a transaction with
    SET LOCAL synchronous_commit = 'off'.
    In 'old' build: conn.executemany with single-row INSERTs and default synchronous_commit.
    """
    if not events:
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        records = [
            (
                e["code"],
                e["timestamp"],
                e.get("user_agent", ""),
                e.get("referrer", ""),
            )
            for e in events
        ]
        if settings.analytics_build == "old":
            await conn.executemany(
                "INSERT INTO clicks (code, clicked_at, user_agent, referrer)"
                " VALUES ($1, $2, $3, $4)",
                records,
            )
        else:
            async with conn.transaction():
                await conn.execute("SET LOCAL synchronous_commit = 'off'")
                await conn.copy_records_to_table(
                    "clicks",
                    records=records,
                    columns=["code", "clicked_at", "user_agent", "referrer"],
                )


async def purge_expired(batch_size: int = 500) -> int:
    """Delete a batch of expired URLs. Returns the number of rows deleted."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            DELETE FROM urls
            WHERE code IN (
                SELECT code FROM urls
                WHERE expires_at IS NOT NULL AND expires_at < now()
                LIMIT $1
            )
            """,
            batch_size,
        )
    try:
        return int(result.split()[-1])
    except Exception:
        return 0


async def check_db() -> bool:
    """Health check: run a trivial query."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return True
    except Exception:
        return False
