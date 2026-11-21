-- Migration 001: initial schema (fully idempotent — safe to re-run)
--
-- PostgreSQL tuning notes (applied via docker-compose command flags):
--   shared_buffers=256MB          : 25% of RAM — keeps hot index/data pages in memory
--   effective_cache_size=512MB    : planner hint; set to ~50-75% RAM
--   max_connections=100           : pool-based apps rarely need more; each conn ~5-10 MB
--   autovacuum_vacuum_scale_factor: per-table override below — 1% vs default 20%
--   autovacuum_analyze_scale_factor: 0.5% vs default 10%
--   wal_buffers=16MB              : reduces WAL write latency on insert bursts
--   checkpoint_completion_target=0.9: spreads fsync I/O over 90% of checkpoint interval

-- Sequence for block-based ID generation.
-- INCREMENT BY matches Settings.id_block_size (100000).
-- Each API process calls nextval() once and owns the entire block in memory.
CREATE SEQUENCE IF NOT EXISTS url_id_seq
    START 1
    INCREMENT BY 100000
    NO MAXVALUE
    CACHE 1;

-- Primary URL mapping table
CREATE TABLE IF NOT EXISTS urls (
    code        TEXT        PRIMARY KEY,
    long_url    TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ
);

-- Partial index for expiry purge queries (only rows with a TTL)
CREATE INDEX IF NOT EXISTS idx_urls_expires_at
    ON urls (expires_at)
    WHERE expires_at IS NOT NULL;

-- Analytics click table
CREATE TABLE IF NOT EXISTS clicks (
    id          BIGSERIAL   PRIMARY KEY,
    code        TEXT        NOT NULL,
    clicked_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_agent  TEXT,
    referrer    TEXT
);

CREATE INDEX IF NOT EXISTS idx_clicks_code ON clicks (code);

-- Per-table autovacuum tuning for the insert-heavy urls table:
--   vacuum_scale_factor=0.01  : vacuum after 1% of rows changed  (default 0.20)
--   analyze_scale_factor=0.005: analyze after 0.5% changed       (default 0.10)
--   vacuum_cost_delay=2       : slow vacuum I/O so it doesn't compete with reads
ALTER TABLE urls SET (
    autovacuum_vacuum_scale_factor   = 0.01,
    autovacuum_analyze_scale_factor  = 0.005,
    autovacuum_vacuum_cost_delay     = 2
);
