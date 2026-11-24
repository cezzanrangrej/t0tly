#!/usr/bin/env python3
"""
API-level seeder for URL shortener.

NOTE:
For high-volume load testing datasets (e.g. 100,000 URLs), use `loadtest/seed_db.py`
which directly and idempotently seeds PostgreSQL and pre-warms Redis.
This script is intended for functional testing or small-scale API checks, as the
`/shorten` endpoint is intentionally rate-limited per client IP (default 10 req/min).

Usage:
    python loadtest/seed_codes.py --count 10 --base-url http://localhost --output loadtest/codes.txt
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import httpx

SEED_URLS = [
    "https://example.com/",
    "https://github.com/",
    "https://python.org/",
    "https://fastapi.tiangolo.com/",
    "https://docs.python.org/3/",
    "https://redis.io/",
    "https://postgresql.org/",
    "https://nginx.org/",
    "https://prometheus.io/",
    "https://grafana.com/",
]


async def seed(base_url: str, count: int, concurrency: int = 5) -> list[str]:
    codes: list[str] = []
    sem = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        async def create_one(i: int) -> str | None:
            async with sem:
                url = SEED_URLS[i % len(SEED_URLS)] + f"?seed={i}"
                try:
                    # Use unique IP header to avoid token bucket rate-limiting on testing
                    headers = {"X-Real-IP": f"10.99.{i // 256}.{i % 256 + 1}"}
                    r = await client.post("/shorten", json={"url": url}, headers=headers)
                    if r.status_code == 201:
                        return r.json().get("code")
                    elif r.status_code == 429:
                        print(f"[seed] rate-limited at i={i}", file=sys.stderr)
                except Exception as exc:
                    print(f"[seed] error at i={i}: {exc}", file=sys.stderr)
                return None

        results = await asyncio.gather(*[create_one(i) for i in range(count)])
        codes = [c for c in results if c]

    return codes


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed URL shortener codes via HTTP API")
    parser.add_argument("--count", type=int, default=10, help="Number of codes to create")
    parser.add_argument("--base-url", default="http://localhost", help="Base URL of the service")
    parser.add_argument("--concurrency", type=int, default=5, help="Concurrent requests")
    parser.add_argument("--output", default=None, help="Optional output file path (e.g. loadtest/codes.txt)")
    args = parser.parse_args()

    codes = asyncio.run(seed(args.base_url, args.count, args.concurrency))
    print(",".join(codes))
    print(f"Seeded {len(codes)}/{args.count} codes via API", file=sys.stderr)

    if args.output and codes:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write("\n".join(codes) + "\n")
        print(f"Saved {len(codes)} codes to {args.output}", file=sys.stderr)

    if len(codes) < args.count * 0.9:
        print("WARNING: fewer than 90% of codes were created — check rate limits", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
