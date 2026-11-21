from __future__ import annotations
from prometheus_client import Counter, Histogram, Gauge

REQUEST_COUNT = Counter(
    "urlshort_requests_total",
    "Total HTTP requests",
    ["method", "route", "status"],
)

REQUEST_LATENCY = Histogram(
    "urlshort_request_latency_seconds",
    "HTTP request latency",
    ["route"],
    buckets=[0.001, 0.005, 0.010, 0.025, 0.050, 0.100, 0.250, 0.500, 1.0],
)

CACHE_HIT = Counter(
    "urlshort_cache_hits_total",
    "Cache hits by layer",
    ["layer"],  # local, redis, neg, db
)

CACHE_MISS = Counter(
    "urlshort_cache_misses_total",
    "Cache misses by layer",
    ["layer"],
)

ANALYTICS_QUEUE_DEPTH = Gauge(
    "urlshort_analytics_queue_depth",
    "Current analytics queue depth",
)

DB_POOL_SIZE = Gauge(
    "urlshort_db_pool_size",
    "DB connection pool configured max size",
)

REDIRECT_TOTAL = Counter(
    "urlshort_redirects_total",
    "Total redirects served",
    ["code"],  # 301, 302, 404
)
