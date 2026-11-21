from __future__ import annotations
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import asyncpg
import orjson
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from pydantic import BaseModel, field_validator
from redis.asyncio import Redis, ConnectionPool

from app.analytics import enqueue_click, start_flush_task, stop_flush_task
from app.cache import (
    get_url, set_url, set_negative, single_flight_lookup,
    check_redis, set_redis as cache_set_redis,
)
from app.config import settings
from app.idgen import allocator, id_to_code
from app.metrics import REQUEST_COUNT, REQUEST_LATENCY, REDIRECT_TOTAL
from app.ratelimit import check_rate_limit, set_redis as rl_set_redis
from app.repo import (
    check_db, get_url_with_expiry, insert_url,
    get_click_count, insert_clicks_batch, purge_expired, set_pool, url_exists,
)
from app.validation import validate_url, validate_alias, ValidationError

logging.basicConfig(
    level=logging.INFO,
    format=(
        '{"time":"%(asctime)s","level":"%(levelname)s",'
        '"name":"%(name)s","msg":"%(message)s"}'
    ),
)
logger = logging.getLogger(__name__)

# Pre-bind Prometheus metrics for maximum hot-path speed
REDIRECT_LATENCY_HIST = REQUEST_LATENCY.labels(route="/{code}")
REDIRECT_COUNT_302 = REQUEST_COUNT.labels(method="GET", route="/{code}", status="302")
REDIRECT_COUNT_301 = REQUEST_COUNT.labels(method="GET", route="/{code}", status="301")
REDIRECT_COUNT_404 = REQUEST_COUNT.labels(method="GET", route="/{code}", status="404")
SHORTEN_COUNT_201 = REQUEST_COUNT.labels(method="POST", route="/shorten", status="201")
SHORTEN_COUNT_429 = REQUEST_COUNT.labels(method="POST", route="/shorten", status="429")
SHORTEN_COUNT_409 = REQUEST_COUNT.labels(method="POST", route="/shorten", status="409")
SHORTEN_LATENCY_HIST = REQUEST_LATENCY.labels(route="/shorten")


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up URL shortener")

    # DB pool
    pool = await asyncpg.create_pool(
        dsn=settings.postgres_dsn,
        min_size=settings.db_min_pool,
        max_size=settings.db_max_pool,
        command_timeout=5,
    )
    set_pool(pool)
    allocator.set_pool(pool)

    # Idempotent SQL migrations
    mig_dir = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "migrations")
    )
    if os.path.isdir(mig_dir):
        for fname in sorted(os.listdir(mig_dir)):
            if fname.endswith(".sql"):
                sql = open(os.path.join(mig_dir, fname)).read()
                async with pool.acquire() as conn:
                    await conn.execute(sql)
                logger.info("Applied migration: %s", fname)

    # Ensure sequence INCREMENT matches config
    async with pool.acquire() as conn:
        await conn.execute(
            f"ALTER SEQUENCE url_id_seq INCREMENT BY {settings.id_block_size}"
        )

    # Redis connection pool
    redis_pool = ConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        decode_responses=True,
        socket_connect_timeout=settings.redis_timeout,
        socket_timeout=settings.redis_timeout,
    )
    redis_client = Redis(connection_pool=redis_pool)
    cache_set_redis(redis_client)
    rl_set_redis(redis_client)

    # Analytics background flush
    start_flush_task(insert_clicks_batch)

    # Background expiry purge every 60 s
    async def _purge_loop():
        while True:
            await asyncio.sleep(60)
            try:
                n = await purge_expired()
                if n:
                    logger.info("Purged %d expired URLs", n)
            except Exception as exc:
                logger.warning("Purge error: %s", exc)

    purge_task = asyncio.create_task(_purge_loop())

    logger.info("Startup complete — serving on %s:%s", settings.host, settings.port)
    try:
        yield  # ← app runs here
    finally:
        logger.info("Shutting down gracefully")
        purge_task.cancel()
        try:
            await purge_task
        except (asyncio.CancelledError, Exception):
            pass
        await stop_flush_task()
        await redis_client.aclose()
        await pool.close()
        logger.info("Shutdown complete")


# ── App factory ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="URL Shortener",
    version="1.0.0",
    lifespan=lifespan,
    default_response_class=JSONResponse,
)


# ── Request / response models ─────────────────────────────────────────────────

class ShortenRequest(BaseModel):
    url: str
    alias: Optional[str] = None
    ttl_seconds: Optional[int] = None

    @field_validator("url")
    @classmethod
    def _validate_url(cls, v: str) -> str:
        try:
            return validate_url(v)
        except ValidationError as e:
            raise ValueError(str(e))

    @field_validator("alias")
    @classmethod
    def _validate_alias(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        try:
            return validate_alias(v)
        except ValidationError as e:
            raise ValueError(str(e))


class ShortenResponse(BaseModel):
    code: str
    short_url: str
    expires_at: Optional[str] = None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/shorten", response_model=ShortenResponse, status_code=201)
async def shorten(req: Request, body: ShortenRequest):
    t0 = time.perf_counter()
    # Rate limiting (Redis token bucket; nginx is the second layer)
    client_ip = (
        req.headers.get("X-Real-IP")
        or (req.client.host if req.client else "unknown")
    )
    allowed, retry_after = await check_rate_limit(client_ip)
    if not allowed:
        SHORTEN_COUNT_429.inc()
        SHORTEN_LATENCY_HIST.observe(time.perf_counter() - t0)
        return Response(
            content=orjson.dumps({"detail": "Rate limit exceeded"}),
            status_code=429,
            headers={
                "Retry-After": str(retry_after),
                "Content-Type": "application/json",
            },
        )

    # Determine code: custom alias or auto-generated
    if body.alias:
        code = body.alias
    else:
        seq_id = await allocator.next_id()
        code = id_to_code(seq_id)

    # Compute expiry timestamp
    expires_at = None
    expires_at_str = None
    if body.ttl_seconds is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=body.ttl_seconds)
        expires_at_str = expires_at.isoformat()

    # INSERT — PK constraint enforces uniqueness (alias conflict → 409)
    ok = await insert_url(code, body.url, expires_at)
    if not ok:
        SHORTEN_COUNT_409.inc()
        SHORTEN_LATENCY_HIST.observe(time.perf_counter() - t0)
        raise HTTPException(status_code=409, detail=f"Code '{code}' already exists")

    # Warm caches immediately so the first redirect is a cache hit
    await set_url(code, body.url, expires_at.timestamp() if expires_at else None)

    short_url = f"{settings.base_url.rstrip('/')}/{code}"
    SHORTEN_COUNT_201.inc()
    SHORTEN_LATENCY_HIST.observe(time.perf_counter() - t0)
    return ShortenResponse(code=code, short_url=short_url, expires_at=expires_at_str)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    db_ok = await check_db()
    redis_ok = await check_redis()
    healthy = db_ok and redis_ok
    return Response(
        content=orjson.dumps({
            "db": db_ok,
            "redis": redis_ok,
            "status": "ok" if healthy else "degraded",
        }),
        status_code=200 if healthy else 503,
        media_type="application/json",
    )


@app.get("/metrics")
async def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/api/stats/{code}")
async def stats(code: str):
    if not await url_exists(code):
        raise HTTPException(status_code=404, detail="Code not found")
    count = await get_click_count(code)
    return {"code": code, "clicks": count}


@app.get("/{code}")
async def redirect(code: str, request: Request):
    t0 = time.perf_counter()

    # Guard reserved route names
    if code in ("shorten", "health", "ready", "metrics", "api", "favicon.ico", "robots.txt"):
        REDIRECT_COUNT_404.inc()
        raise HTTPException(status_code=404)

    # 1. Try caches (L1 TTLCache → neg cache → L2 Redis)
    long_url = await get_url(code)

    # 2. Single-flight DB lookup on cache miss
    if long_url is None:
        async def _fetch() -> Optional[str]:
            url, exp = await get_url_with_expiry(code)
            if url:
                await set_url(code, url, exp)
            else:
                await set_negative(code)
            return url

        long_url = await single_flight_lookup(code, _fetch)

    if long_url is None:
        REDIRECT_TOTAL.labels(code="404").inc()
        REDIRECT_COUNT_404.inc()
        REDIRECT_LATENCY_HIST.observe(time.perf_counter() - t0)
        raise HTTPException(status_code=404, detail="Short code not found or expired")

    # 3. Record click asynchronously (drop if queue full — never blocks)
    enqueue_click(
        code,
        request.headers.get("user-agent", ""),
        request.headers.get("referer", ""),
    )

    if settings.redirect_code == 302:
        REDIRECT_COUNT_302.inc()
    else:
        REDIRECT_COUNT_301.inc()
    REDIRECT_TOTAL.labels(code=str(settings.redirect_code)).inc()
    REDIRECT_LATENCY_HIST.observe(time.perf_counter() - t0)

    # Raw Response with Location header on the hot path
    return Response(
        status_code=settings.redirect_code,
        headers={
            "Location": long_url,
            "Cache-Control": "public, max-age=60",
        },
    )
