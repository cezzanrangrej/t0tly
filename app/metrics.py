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

ANALYTICS_DROPPED_TOTAL = Counter(
    "urlshort_analytics_dropped_clicks_total",
    "Total click events dropped due to queue overflow",
)

ANALYTICS_FLUSH_FAILURES_TOTAL = Counter(
    "urlshort_analytics_flush_failures_total",
    "Total click analytics flush failures or timeouts",
)

ANALYTICS_FLUSH_BATCH_SIZE = Histogram(
    "urlshort_analytics_flush_batch_size",
    "Batch size written by analytics flush",
    buckets=[50, 100, 250, 500, 1000, 2000, 5000],
)

ANALYTICS_FLUSH_DURATION = Histogram(
    "urlshort_analytics_flush_duration_seconds",
    "Time taken to flush a batch to PostgreSQL",
    buckets=[0.001, 0.005, 0.010, 0.025, 0.050, 0.100, 0.250, 0.500, 1.0, 3.0],
)

EVENT_LOOP_LAG = Gauge(
    "urlshort_event_loop_lag_seconds",
    "Current event loop scheduling lag in seconds",
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
