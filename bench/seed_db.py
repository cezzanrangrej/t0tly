#!/usr/bin/env python3
"""
Production-quality database seeder for load testing.

Requirements & Design:
1. Seeds sequence IDs 1 through `count` (default: 100,000).
2. Uses deterministic Feistel cipher (app.idgen.id_to_code).
3. Non-destructive & Idempotent:
   - Uses temporary staging table `staging_urls`.
   - Detects and reports any unexpected primary-key collisions (same code, different URL).
   - Inserts new records via `ON CONFLICT (code) DO NOTHING`.
   - Preserves all pre-existing records.
4. Safe Sequence Advancement:
   - Reads current `last_value` and `is_called` from `url_id_seq`.
   - Calculates the block ceiling covering `count` (e.g. ceil(count / 100000) * 100000).
   - Ensures `target_seq = max(curr_seq, seeded_ceiling)`.
   - Sets `setval('url_id_seq', target_seq, true)`.
   - Never moves sequence backwards.
   - Ensures subsequent API allocation starts strictly at `target_seq + 1`,
     which is strictly after all seeded IDs and previously reserved blocks.
5. Exact Redis Cache Serialization:
   - Uses `orjson.dumps({"long_url": target_url}).decode("utf-8")` matching `app.cache`.
   - Pre-warms top `min(warm_cache_count, count)` keys.
6. Verified `loadtest/codes.txt`:
   - Writes only verified codes confirmed to exist in PostgreSQL.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
import re
import sys
import time
from typing import Optional

import asyncpg
import orjson
import redis.asyncio as aioredis

from app.config import settings
from app.idgen import id_to_code


def mask_dsn(dsn: str) -> str:
    """Mask password in connection string for safe logging."""
    return re.sub(r":([^@]+)@", ":****@", dsn)


async def seed(
    count: int = 100_000,
    warm_cache_count: int = 5_000,
    postgres_dsn: Optional[str] = None,
    redis_url: Optional[str] = None,
    output_path: str = "bench/codes.txt",
) -> None:
    pg_dsn = postgres_dsn or os.getenv("POSTGRES_DSN") or settings.postgres_dsn
    rd_url = redis_url or os.getenv("REDIS_URL") or settings.redis_url

    print("==================================================")
    print("      URL Shortener Database Seeder Starting      ")
    print("==================================================")
    print(f"PostgreSQL target: {mask_dsn(pg_dsn)}")
    print(f"Redis target:      {mask_dsn(rd_url)}")
    print(f"Seeding count:     {count:,} URLs (IDs 1 through {count:,})")
    print(f"Redis warm count:  {min(warm_cache_count, count):,} hot keys")
    print(f"Output file:       {output_path}")
    print("--------------------------------------------------")

    t_start = time.time()

    # 1. Connect to PostgreSQL
    try:
        conn = await asyncpg.connect(pg_dsn, timeout=10.0)
    except Exception as exc:
        print(f"ERROR: Failed to connect to PostgreSQL: {exc}", file=sys.stderr)
        sys.exit(1)

    print("Connected to PostgreSQL successfully.")

    # 2. Check current sequence status and active sessions
    seq_row = await conn.fetchrow("SELECT last_value, is_called FROM url_id_seq;")
    curr_seq = int(seq_row["last_value"])
    is_called = bool(seq_row["is_called"])
    print(f"Current PostgreSQL sequence 'url_id_seq': last_value={curr_seq:,}, is_called={is_called}")

    # Check for active API connections
    active_conns = await conn.fetchval(
        "SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE '%uvicorn%' OR application_name LIKE '%asyncpg%';"
    )
    if active_conns and active_conns > 1:
        print(f"NOTE: Detected {active_conns} active database client sessions.")

    # 3. Generate records for IDs 1 through count
    print(f"Generating {count:,} codes using Feistel permutation (IDs 1..{count:,})...")
    t_gen_start = time.time()
    records = []
    generated_codes = []
    for i in range(1, count + 1):
        code = id_to_code(i)
        generated_codes.append(code)
        records.append((code, f"https://example.com/article/{i}?ref=shortener", None))
    t_gen = time.time() - t_gen_start
    print(f"Generated {count:,} codes in {t_gen:.2f}s (Sample: {generated_codes[0]}, {generated_codes[1] if count > 1 else ''})")

    # 4. Safe bulk insertion via temporary staging table with collision checks
    print("Populating staging table and validating collisions...")
    t_insert_start = time.time()

    async with conn.transaction():
        await conn.execute("""
            CREATE TEMP TABLE staging_urls (
                code TEXT PRIMARY KEY,
                long_url TEXT NOT NULL,
                expires_at TIMESTAMPTZ
            ) ON COMMIT DROP;
        """)

        # Fast binary COPY into staging
        await conn.copy_records_to_table(
            "staging_urls",
            records=records,
            columns=["code", "long_url", "expires_at"],
        )

        # Check for unexpected primary-key collisions (same code, different target URL)
        conflict_rows = await conn.fetch("""
            SELECT s.code, s.long_url AS new_url, u.long_url AS existing_url
            FROM staging_urls s
            JOIN urls u ON s.code = u.code
            WHERE s.long_url <> u.long_url;
        """)

        if conflict_rows:
            print(f"WARNING: Found {len(conflict_rows)} unexpected primary-key conflicts with existing data:", file=sys.stderr)
            for r in conflict_rows[:5]:
                print(f"  • Code '{r['code']}': existing='{r['existing_url']}', attempted='{r['new_url']}'", file=sys.stderr)
            if len(conflict_rows) > 5:
                print(f"  ... and {len(conflict_rows) - 5} more conflicts.", file=sys.stderr)
            print("Preserving existing URLs (ON CONFLICT DO NOTHING will not overwrite them).", file=sys.stderr)
        else:
            print("Collision check passed: 0 unexpected URL conflicts detected.")

        # Safe insert with ON CONFLICT DO NOTHING
        row = await conn.fetchrow("""
            WITH inserted AS (
                INSERT INTO urls (code, long_url, expires_at)
                SELECT code, long_url, expires_at FROM staging_urls
                ON CONFLICT (code) DO NOTHING
                RETURNING code
            )
            SELECT count(*) AS new_count FROM inserted;
        """)
        new_count = row["new_count"]

    t_insert = time.time() - t_insert_start
    total_in_db = await conn.fetchval("SELECT count(*) FROM urls;")
    identical_existing = count - new_count - len(conflict_rows)
    print(f"PostgreSQL insert complete in {t_insert:.2f}s:")
    print(f"  • Newly inserted:          {new_count:,}")
    print(f"  • Identical repeat records: {identical_existing:,}")
    if conflict_rows:
        print(f"  • Skipped conflicts:       {len(conflict_rows):,}")
    print(f"  • Total URLs in database:  {total_in_db:,}")

    # 5. Safe sequence advancement
    # Calculate block ceiling covering all seeded IDs
    block_size = settings.id_block_size
    blocks_needed = math.ceil(count / block_size)
    seeded_ceiling = blocks_needed * block_size

    # Ensure sequence never moves backwards and covers all seeded IDs
    target_seq = max(curr_seq, seeded_ceiling)
    await conn.execute(f"SELECT setval('url_id_seq', {target_seq}, true);")

    updated_seq = await conn.fetchval("SELECT last_value FROM url_id_seq;")
    next_alloc_start = updated_seq + 1
    print(f"PostgreSQL sequence advancement verified:")
    print(f"  • Seeded IDs range:       1 to {count:,}")
    print(f"  • Previous sequence value: {curr_seq:,}")
    print(f"  • Updated sequence value:  {updated_seq:,} (is_called=true)")
    print(f"  • Next API allocation:     {next_alloc_start:,} to {next_alloc_start + block_size - 1:,}")
    print(f"  • Safe gap:                Strictly after all seeded IDs and previous blocks")

    # 6. Retrieve verified seeded records from PostgreSQL
    print(f"Verifying {count:,} seeded records in PostgreSQL...")
    # Fetch records matching our generated codes
    verified_rows = await conn.fetch("""
        SELECT code, long_url
        FROM urls
        WHERE code = ANY($1::text[]);
    """, generated_codes)

    verified_map = {r["code"]: r["long_url"] for r in verified_rows}
    await conn.close()

    # Preserve exact generation order in codes.txt
    valid_codes = [c for c in generated_codes if c in verified_map]
    print(f"Verified {len(valid_codes):,} of {count:,} codes exist in PostgreSQL.")

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(valid_codes) + "\n")
    print(f"Saved {len(valid_codes):,} verified codes to {output_path}")

    # 7. Pre-warm Redis cache using exact app.cache serialization
    keys_to_warm = min(warm_cache_count, len(valid_codes))
    print(f"Pre-warming Redis cache with top {keys_to_warm:,} hot keys...")
    try:
        r = aioredis.from_url(rd_url, socket_timeout=5.0)
        await r.ping()
        pipe = r.pipeline(transaction=False)
        for c in valid_codes[:keys_to_warm]:
            target_url = verified_map[c]
            # Exact serialization format matching app.cache.set_url
            payload = orjson.dumps({"long_url": target_url}).decode("utf-8")
            pipe.set(f"url:{c}", payload, ex=settings.cache_ttl_seconds)

        await pipe.execute()
        redis_size = await r.dbsize()
        await r.aclose()
        print(f"Redis pre-warming complete. Total Redis keys: {redis_size:,}")
    except Exception as exc:
        print(f"WARNING: Redis pre-warming failed: {exc}", file=sys.stderr)
        print("Redirects will still function by falling back to PostgreSQL.", file=sys.stderr)

    total_time = time.time() - t_start
    print("--------------------------------------------------")
    print(f"Seeding completed successfully in {total_time:.2f}s!")
    print("==================================================")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed database for URL shortener load tests")
    parser.add_argument("--count", type=int, default=100_000, help="Number of URLs to seed (default: 100,000)")
    parser.add_argument("--warm-cache", type=int, default=5_000, help="Number of hot keys to pre-warm in Redis (default: 5,000)")
    parser.add_argument("--postgres-dsn", default=None, help="PostgreSQL connection string override")
    parser.add_argument("--redis-url", default=None, help="Redis connection URL override")
    parser.add_argument("--output", default="bench/codes.txt", help="Output path for codes.txt")
    args = parser.parse_args()

    asyncio.run(
        seed(
            count=args.count,
            warm_cache_count=args.warm_cache,
            postgres_dsn=args.postgres_dsn,
            redis_url=args.redis_url,
            output_path=args.output,
        )
    )


if __name__ == "__main__":
    main()
