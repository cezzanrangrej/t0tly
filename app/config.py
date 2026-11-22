from __future__ import annotations
import secrets
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 1
    redirect_code: int = Field(302, ge=301, le=302)
    base_url: str = "http://127.0.0.1:8000"

    # Postgres
    postgres_dsn: str = "postgresql://urlshort:urlshort@127.0.0.1:5432/urlshort"
    db_min_pool: int = 2
    db_max_pool: int = 10

    # Redis
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_max_connections: int = 20
    redis_timeout: float = 0.5         # seconds
    cache_ttl_seconds: int = 86400     # 24 h Redis TTL
    local_cache_ttl: int = 180          # seconds (L1 hot cache TTL for high-QPS)
    local_cache_max: int = 50_000

    # ID generation
    id_block_size: int = 100_000
    feistel_key1: str = "deadbeef01234567"
    feistel_key2: str = "cafebabe89abcdef"
    feistel_key3: str = "0011223344556677"
    feistel_key4: str = "8899aabbccddeeff"

    # Rate limiting
    rate_limit_creates: int = 10       # requests per window
    rate_limit_window: int = 60        # seconds

    # Analytics
    analytics_queue_size: int = 50_000
    analytics_batch_size: int = 1_000
    analytics_flush_interval: float = 2.0

    # Negative cache
    negative_cache_ttl: int = 30       # seconds
    negative_cache_max: int = 5_000

    # Reserved codes (cannot be used as aliases or will conflict with routes)
    reserved_codes: list[str] = [
        "shorten", "health", "ready", "metrics", "api",
        "favicon.ico", "robots.txt",
    ]

    @field_validator("redirect_code")
    @classmethod
    def validate_redirect(cls, v: int) -> int:
        if v not in (301, 302):
            raise ValueError("redirect_code must be 301 or 302")
        return v


settings = Settings()
