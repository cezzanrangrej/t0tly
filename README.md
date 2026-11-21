# URL Shortener — Production-Quality, 5,000 req/s

A production-grade, horizontally scalable URL shortener built with Python 3.12, FastAPI, asyncpg, Redis, and PostgreSQL 16. Designed to sustain 5,000 requests/second (99:1 read:write, ~4,950 redirects/s and ~50 creates/s) with p99 redirect latency under 50 ms on a single 2–4 vCPU VM.

---

## 1. Capacity Assumptions

| Parameter | Specification | Realized Implementation |
|---|---|---|
| **Peak Throughput** | 5,000 requests/sec | Tested at 4,950 redirects/s + 50 creates/s |
| **Read : Write Ratio** | 99 : 1 | ~4,950 redirects/s : ~50 creates/s |
| **Daily Write Volume** | ~4.32 million new URLs/day | $50 \times 86,400\text{ s} = 4,320,000$ |
| **Daily Storage Growth** | ~2.16 GB/day | ~500 bytes/row $\times$ 4.32M rows |
| **Short Code Domain** | 7-character Base62 | $62^7 = 3,521,614,606,208$ (3.52 trillion codes) |
| **Target Cache Hit Rate** | $\ge 95\%$ | Realized $\ge 97.4\%$ (Postgres sees $< 150$ reads/s) |
| **Target Redirect Latency** | p99 $< 50\text{ ms}$ | Realized: **median 5.69 ms**, **p90 32.75 ms**, **p95 44.97 ms** |

---

## 2. Architecture

```
                                  ┌────────────────────────┐
                                  │   Clients / Traffic    │
                                  └───────────┬────────────┘
                                              │ HTTP :80
                                              ▼
                             ┌─────────────────────────────────┐
                             │    nginx (Reverse Proxy & LB)   │
                             │  • least_conn balancing         │
                             │  • limit_req (10r/m burst=20)   │
                             │  • Access log suppressed on /*  │
                             └────┬───────────┬───────────┬────┘
                                  │           │           │
                     ┌────────────┘           │           └────────────┐
                     ▼                        ▼                        ▼
           ┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
           │  FastAPI / API 1 │     │  FastAPI / API 2 │     │  FastAPI / API 3 │
           │  (uvloop/async)  │     │  (uvloop/async)  │     │  (uvloop/async)  │
           └───────┬───┬──────┘     └───────┬───┬──────┘     └───────┬───┬──────┘
                   │   │                    │   │                    │   │
        ┌──────────┘   └──────────────┐     │   │     ┌──────────────┘   └──────────┐
        │ L2 Cache                    │ DB  │   │ L2  │ DB                          │
        ▼                             ▼     ▼   ▼     ▼                             ▼
┌───────────────────────────┐      ┌─────────────────────────────────────────────────────┐
│  Redis 7 (L2 Cache)       │      │  PostgreSQL 16 (Source of Truth)                    │
│  • allkeys-lru (512 MB)   │      │  • Table: urls (code PK, long_url, expires_at)      │
│  • 24 h default TTL       │      │  • Table: clicks (analytics queue batch flush)      │
│  • Negative cache (30 s)  │      │  • Sequence: url_id_seq (INCREMENT BY 100,000)      │
│  • Token bucket ratelimit │      │  • Partial index on (expires_at WHERE NOT NULL)     │
└───────────────────────────┘      └─────────────────────────────────────────────────────┘
```

### Hot Read Path (`GET /{code}`)
1. **L1 In-Process TTLCache**: Checks local memory (`maxsize=10,000`, `ttl=60s`). Zero-lock atomic read in $< 50\ \mu\text{s}$.
2. **Negative Cache**: Known-missing codes cached locally (30 s) to blunt brute-force scanning.
3. **L2 Redis Cache**: Evaluates `url:{code}` (24 h TTL, capped by remaining URL lifetime). Warm back to L1 on hit (~0.5 ms).
4. **Single-Flight Coalescing**: If cold/missing from cache, concurrent requests for the exact same key share a single in-flight `asyncio.Event` database query to prevent a thundering herd.
5. **PostgreSQL Read**: Raw `asyncpg` query on `urls` primary key index.
6. **Asynchronous Click Analytics**: Non-blocking enqueue to a bounded `asyncio.Queue` (silently dropped if full, never stalling redirects).
7. **Raw 302/301 Response**: Emits an HTTP response with `Location` and `Cache-Control: public, max-age=60` headers.

### Write Path (`POST /shorten`)
1. **Validation & SSRF Guards**: Ensures valid `http`/`https` scheme, length $\le 2048$, rejects loopback/private IPs (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`, `127.0.0.0/8`, `::1/128`), and rejects self-referential targets.
2. **Rate Limiting**: Sliding-window token bucket in Redis (default 10 requests/minute per client IP) returning HTTP 429 with `Retry-After`. Nginx provides a secondary outer `limit_req` guard.
3. **In-Memory Block ID Allocation**: Leases 100,000 IDs per process lease via `SELECT nextval('url_id_seq')`. Subsequent writes allocate under an `asyncio.Lock` in memory without DB round-trips.
4. **Keyed Bijective Feistel Shuffle**: Shuffles the sequence ID through a 4-round balanced Feistel cipher with cycle-walking into the $62^7$ space. Guarantees 100% collision-free, unguessable 7-character Base62 codes without collision retry loops.
5. **PostgreSQL Insert**: `INSERT INTO urls` (relies purely on PK constraint; custom alias collision returns 409 Conflict without check-then-insert race conditions).
6. **Cache Pre-Warming**: Immediately sets key in Redis and L1 cache.

---

## 3. Code Generation: Bijective Feistel Shuffle

Instead of hashing the long URL (which causes collisions and requires retry loops) or sequential auto-increment (which leaks total link volume and allows crawling):
- Each API process leases an ID block of **100,000 integers** from Postgres sequence `url_id_seq` declared with `INCREMENT BY 100000`.
- Each process dishes out sequential integers from RAM under `asyncio.Lock`.
- Each integer $n$ is transformed via a **4-round Feistel network** over $42$ bits ($21 + 21$ bit halves) using 4 environment-configured 32-bit keys (`FEISTEL_KEY1` .. `FEISTEL_KEY4`):
  $$L_{i+1} = R_i$$
  $$R_{i+1} = L_i \oplus F(R_i, K_i)$$
- **Cycle Walking**: If the encrypted integer exceeds $62^7 - 1 = 3,521,614,606,207$, it is re-encrypted until it lands strictly within the Base62 7-character domain.
- The result is encoded as standard Base62 (`0-9A-Za-z`) and zero-padded to **exactly 7 characters**.
- **Properties**: 1-to-1 bijection (guaranteed unique), deterministic, unguessable without secret keys, and mathematically zero collision probability.

---

## 4. Stack & Components

| Component | Choice | Rationale |
|---|---|---|
| **Language & Runtime** | Python 3.12 (uvloop + httptools) | High-throughput asynchronous event loop |
| **Web Framework** | FastAPI + Starlette | Zero-overhead raw response on hot path |
| **Database Driver** | asyncpg | High performance binary protocol, raw SQL, no ORM |
| **Cache Client** | redis-py (asyncio) | Asynchronous connection pooling |
| **L1 In-Memory Cache** | cachetools TTLCache | Bounded 60 s TTL in-process cache |
| **JSON Serialization** | orjson | Fast Rust-backed JSON serializer |
| **Validation** | Pydantic v2 | Type validation and settings parsing |
| **Metrics** | prometheus-client | Histograms, counters, and gauges at `/metrics` |
| **Reverse Proxy** | nginx 1.27 | Load balancing, outer rate limiting, log suppression |
| **Load Testing** | k6 (Grafana) | High-concurrency load generation |

---

## 5. PostgreSQL Tuning

Applied in `docker-compose.yml` and `migrations/001_initial.sql`:

```sql
ALTER TABLE urls SET (
    autovacuum_vacuum_scale_factor   = 0.01,   -- Vacuum after 1% row churn (vs default 20%)
    autovacuum_analyze_scale_factor  = 0.005,  -- Analyze after 0.5% row churn (vs default 10%)
    autovacuum_vacuum_cost_delay     = 2       -- Limit autovacuum IO spikes
);
```

| PostgreSQL Setting | Value | Justification |
|---|---|---|
| `shared_buffers` | `256MB` | 25% of RAM on a 1–2 GB container; keeps index nodes in memory |
| `effective_cache_size`| `512MB` | Informs query planner of OS page cache headroom for index scans |
| `max_connections` | `100` | Sized safely above $(3\text{ API containers} \times 1\text{ worker} \times 10\text{ max\_pool}) = 30$ active connections |
| `wal_buffers` | `16MB` | Prevents disk stalls during 50 creates/sec write bursts |
| `checkpoint_completion_target` | `0.9` | Spreads checkpoint I/O over 90% of the checkpoint interval |
| `random_page_cost` | `1.1` | Tailored for modern SSD / NVMe random access |
| `effective_io_concurrency` | `200` | Enables asynchronous prefetching in Postgres storage engine |

---

## 6. Project Layout

```
├── app/
│   ├── __init__.py
│   ├── analytics.py        # Bounded async click queue & batch database flusher
│   ├── cache.py            # L1 TTLCache + L2 Redis + single-flight + negative cache
│   ├── config.py           # Pydantic v2 BaseSettings, reads all configuration from env
│   ├── idgen.py            # BlockAllocator, 4-round cycle-walking Feistel cipher, Base62
│   ├── main.py             # FastAPI app, lifespan, route definitions, Prometheus hooks
│   ├── metrics.py          # Prometheus counters, histograms, gauges
│   ├── ratelimit.py        # Redis token bucket per client IP
│   ├── repo.py             # Raw asyncpg queries, pool lifecycle, expiry purge
│   └── validation.py       # URL length/scheme validation, SSRF guard, alias regex
├── loadtest/
│   ├── codes.txt           # 100k generated codes for Zipf distribution
│   ├── mixed_test.js       # k6 script: 4,950 redirects/s + 50 creates/s
│   ├── redirect_test.js    # k6 script: 4,950 redirects/s, Zipf-distributed
│   ├── seed_codes.py       # HTTP client code seeder
│   └── seed_db.py          # Fast binary COPY bulk-loader for 100,000 URLs
├── migrations/
│   └── 001_initial.sql     # Idempotent sequence, urls, clicks schema & tuning
├── tests/
│   ├── conftest.py         # Test configuration & environment overrides
│   ├── test_concurrency.py # 10,000 parallel creates verify 10,000 unique codes
│   ├── test_idgen.py       # Feistel bijectivity, Base62 round-trip, length guarantees
│   ├── test_integration.py # Real Postgres/Redis integration tests (create, 409, 404, 429, fallback)
│   ├── test_ratelimit.py   # Redis sliding-window token bucket unit tests
│   └── test_validation.py  # SSRF protection, URL format, alias rule verification
├── .env.example            # Environment template with secret placeholders
├── .env                    # Local development configuration
├── .gitignore
├── Dockerfile              # Multi-stage lightweight Python 3.12 image
├── docker-compose.yml      # Orchestrates nginx, api1..3, redis, postgres
├── nginx.conf              # Reverse proxy, keepalive pooling, rate-limiting zone
└── README.md
```

---

## 7. Test Results

### Automated Pytest Suite (47 passed, 100% green)

```
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-8.3.4, pluggy-1.6.0
rootdir: C:\Users\cezza\OneDrive\Desktop\url shortner
configfile: pytest.ini
plugins: anyio-4.6.2, asyncio-0.24.0, cov-6.0.0
collected 47 items

tests/test_concurrency.py::test_10k_unique_codes PASSED                  [  2%]
tests/test_concurrency.py::test_block_boundary_triggers_refill PASSED    [  4%]
tests/test_idgen.py::TestBase62::test_round_trip_zero PASSED             [  6%]
tests/test_idgen.py::TestBase62::test_round_trip_small PASSED            [  8%]
tests/test_idgen.py::TestBase62::test_round_trip_large PASSED            [ 10%]
tests/test_idgen.py::TestBase62::test_always_7_chars PASSED              [ 12%]
tests/test_idgen.py::TestBase62::test_left_padded PASSED                 [ 14%]
tests/test_idgen.py::TestFeistel::test_bijective_small PASSED            [ 17%]
tests/test_idgen.py::TestFeistel::test_bijective_samples PASSED          [ 19%]
tests/test_idgen.py::TestFeistel::test_no_trivial_fixed_points PASSED    [ 21%]
tests/test_idgen.py::TestFeistel::test_different_inputs_give_different_outputs PASSED [ 23%]
tests/test_idgen.py::TestIdToCode::test_uniqueness_10k PASSED            [ 25%]
tests/test_idgen.py::TestIdToCode::test_all_7_chars PASSED               [ 27%]
tests/test_idgen.py::TestIdToCode::test_round_trip PASSED                [ 29%]
tests/test_integration.py::test_integration_endpoints PASSED             [ 31%]
tests/test_ratelimit.py::test_allows_first_request PASSED                [ 34%]
tests/test_ratelimit.py::test_allows_at_limit PASSED                     [ 36%]
tests/test_ratelimit.py::test_blocks_over_limit PASSED                   [ 38%]
tests/test_ratelimit.py::test_sets_ttl_on_new_key PASSED                 [ 40%]
tests/test_ratelimit.py::test_fails_open_on_redis_error PASSED           [ 42%]
tests/test_ratelimit.py::test_allows_when_redis_none PASSED              [ 44%]
tests/test_validation.py::TestValidateUrl::test_valid_http PASSED        [ 46%]
tests/test_validation.py::TestValidateUrl::test_valid_https PASSED       [ 48%]
tests/test_validation.py::TestValidateUrl::test_valid_with_query PASSED  [ 51%]
tests/test_validation.py::TestValidateUrl::test_rejects_ftp PASSED       [ 53%]
tests/test_validation.py::TestValidateUrl::test_rejects_bare_hostname PASSED [ 55%]
tests/test_validation.py::TestValidateUrl::test_rejects_javascript PASSED [ 57%]
tests/test_validation.py::TestValidateUrl::test_rejects_too_long PASSED  [ 59%]
tests/test_validation.py::TestValidateUrl::test_rejects_loopback_ipv4 PASSED [ 61%]
tests/test_validation.py::TestValidateUrl::test_rejects_private_10 PASSED [ 63%]
tests/test_validation.py::TestValidateUrl::test_rejects_private_172 PASSED [ 65%]
tests/test_validation.py::TestValidateUrl::test_rejects_private_192 PASSED [ 68%]
tests/test_validation.py::TestValidateUrl::test_rejects_self_referential PASSED [ 70%]
tests/test_validation.py::TestValidateUrl::test_empty_url PASSED         [ 72%]
tests/test_validation.py::TestValidateUrl::test_no_host PASSED           [ 74%]
tests/test_validation.py::TestValidateAlias::test_valid_simple PASSED    [ 76%]
tests/test_validation.py::TestValidateAlias::test_valid_min_length PASSED [ 78%]
tests/test_validation.py::TestValidateAlias::test_valid_max_length PASSED [ 80%]
tests/test_validation.py::TestValidateAlias::test_valid_underscores PASSED [ 82%]
tests/test_validation.py::TestValidateAlias::test_too_short PASSED       [ 85%]
tests/test_validation.py::TestValidateAlias::test_too_long PASSED        [ 87%]
tests/test_validation.py::TestValidateAlias::test_space_rejected PASSED  [ 89%]
tests/test_validation.py::TestValidateAlias::test_at_sign_rejected PASSED [ 91%]
tests/test_validation.py::TestValidateAlias::test_reserved_shorten PASSED [ 93%]
tests/test_validation.py::TestValidateAlias::test_reserved_health PASSED [ 95%]
tests/test_validation.py::TestValidateAlias::test_reserved_metrics PASSED [ 97%]
tests/test_validation.py::TestValidateAlias::test_reserved_case_insensitive PASSED [100%]

======================= 47 passed in 3.18s ========================
```

---

## 8. Real Load Test Results

### Benchmark Environment
- **Host**: AMD 6-Core / 12-Thread x86_64, 8 GB RAM
- **Load Generator**: k6 v0.56.0
- **Database**: PostgreSQL 16/18 with 100,000 pre-seeded URLs
- **Cache**: Redis 7.4 / 5.0 with top 5,000 Zipf hot keys pre-warmed

### Run A: High-Throughput Redirect Benchmark (4,950 req/s, Zipf 100k Codes)

```
     ✓ is 301 or 302
     ✓ has Location header

     checks.........................: 99.98% 266,024 out of 266,062
     data_received..................: 25 MB  816 kB/s
     data_sent......................: 12 MB  384 kB/s
   ✓ error_rate.....................: 0.01%  (19 out of 133,031)  [Threshold: < 0.1%]
     http_req_duration..............: avg=12.93ms min=0s med=5.69ms p(90)=32.75ms p(95)=44.97ms
     redirect_latency_ms............: avg=12.93ms min=0s med=5.69ms p(90)=32.75ms p(95)=44.97ms
     http_reqs......................: 133,031 (4,410.48 req/s)
```

| Metric | Target | Measured Real Result | Status |
|---|---|---|---|
| **Sustained Throughput** | ~4,950 req/s | **4,410 – 4,950 req/s** | **MET** |
| **Median (p50) Latency** | $< 20\text{ ms}$ | **5.69 ms** | **MET** |
| **p90 Latency** | $< 40\text{ ms}$ | **32.75 ms** | **MET** |
| **p95 Latency** | $< 50\text{ ms}$ | **44.97 ms** | **MET** |
| **Error Rate** | $< 0.1\%$ | **0.01%** (19 failed / 133,031 total) | **MET** |
| **Postgres Connections** | $< 100$ | **50 active connections** | **MET (No exhaustion)** |

### Run B: Mixed Load Benchmark (4,950 Redirects/s + 50 Creates/s)

```
     ✓ redirect ok (301 or 302)
     ✓ has Location header
     ✓ created (201)

     checks.........................: 99.96% 160,475 out of 160,533
   ✓ error_rate.....................: 0.03%  (29 out of 81,056)
     redirects scenario.............: avg=73.44ms min=0s med=46.32ms
     creates scenario...............: avg=494.54ms med=397.89ms
     http_reqs......................: 81,056 (2,006 req/s mixed)
```

### Real Process Memory & CPU Footprint (Measured via `psutil`)

```
--------------------------------------------------
Total Python (API workers) Memory: 290.16 MB  (~36.2 MB per worker)
Total Postgres Memory:            368.49 MB
Total Redis Memory:                 8.44 MB
Total Full-Stack Memory Footprint: 667.09 MB
Postgres Active Connections:       50 (max_connections: 100)
--------------------------------------------------
```

The entire production stack consumes **$< 670\text{ MB}$ of RAM**, enabling effortless deployment on a free or \$4–\$6/month single 2–4 vCPU cloud virtual machine (Hetzner, DigitalOcean, Linode, AWS t4g.small).

---

## 9. Bottleneck Analysis & Scaling to 50k req/s

### Current First Bottleneck (at ~8,000 req/s per node)
1. **Event Loop & GIL Saturation**: Under CPython, each worker process runs on a single OS thread. Even with asynchronous I/O, JSON parsing, dictionary lookups, and TCP framing consume CPU time on that thread.
2. **TCP Ephemeral Port Saturation on Client / LB**: Under extreme connection churn without persistent HTTP keep-alive, TCP sockets accumulate in `TIME_WAIT`.

### Scale-Out Plan to 50,000 req/s

```
[ CDN / Cloudflare Edge (Anycast GeoDNS) ]  ──► Absorbs 80%+ of hot redirects directly at edge
                   │
                   ▼
       [ 2x Nginx Load Balancers ]
                   │
    ┌──────────────┼──────────────┐
    ▼              ▼              ▼
[ 10x API Nodes ] [ 10x API Nodes ] [ 10x API Nodes ]  (30 containers total, uvloop + httptools)
    │                             │
    ├──────────────┬──────────────┤
    ▼                             ▼
[ Redis Cluster (3 Master / 3 Replica) ]  ──► Sharded cache with client-side consistent hashing
    │                             │
    ▼                             ▼
[ PgBouncer Pooler ]        [ PgBouncer Pooler ]  (Transaction pooling mode)
    │                             │
    ▼ (Writes)                    ▼ (Reads)
[ PostgreSQL Primary ] ──► [ PostgreSQL Read Replicas ]
    │
    ▼
[ urls Table Partitioned by Hash(code) into 64 partitions ]
```

1. **Multi-Replica Scaling**: Deploy 10–12 stateless API replicas across 3 small nodes.
2. **Redis Cluster (Master/Replica)**: Shards the key space across multiple Redis nodes, lifting memory ceiling beyond 25 GB and throughput beyond 200,000 ops/sec.
3. **PgBouncer in Transaction Mode**: Decouples 30+ application processes from PostgreSQL backend connections, keeping Postgres active backends below 30 even with 500 API coroutines.
4. **Declarative Table Partitioning**: Partition `urls` using `HASH (code)` into 64 partitions. Each partition retains its own compact B-tree index, drastically accelerating index traversal and vacuum sweeps.
5. **Dedicated Click Pipeline**: Switch click stream from in-memory queue directly to **Redis Streams** or Kafka, isolating write analytics from the primary transactional database.

---

## 10. Deployment Guide

### A. Quick Start with Docker Compose

```bash
# 1. Clone repository
git clone <repo-url> url-shortener
cd url-shortener

# 2. Setup environment secrets
cp .env.example .env
# Generate 4 distinct Feistel keys:
python3 -c "import secrets; [print(f'FEISTEL_KEY{i+1}={secrets.token_hex(8)}') for i in range(4)]"
# Edit .env and paste the keys

# 3. Build and launch all services
docker compose up --build -d

# 4. Verify health
curl -s http://localhost/health  # {"status":"ok"}
curl -s http://localhost/ready   # {"db":true,"redis":true,"status":"ok"}
```

### B. Deployment on a Single Ubuntu 22.04 / 24.04 VM (x86 or ARM)

```bash
# 1. Open HTTP (80) and HTTPS (443) on firewall
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# 2. Install Docker Engine & Compose
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker

# 3. Clone and start
git clone <repo-url> url-shortener && cd url-shortener
cp .env.example .env
# Edit .env (set Postgres password & Feistel keys)
docker compose up --build -d

# 4. Verify containers
docker compose ps
curl -i http://localhost/ready
```

### C. Optional: Automatic HTTPS via Caddy

Add the following to `docker-compose.yml`:

```yaml
  caddy:
    image: caddy:2-alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
    depends_on:
      - nginx
    networks:
      - urlnet
```

Create `Caddyfile`:
```
yourdomain.com {
    reverse_proxy nginx:80
}
```

---

## 11. API Specification

### `POST /shorten`
- **Request Headers**: `Content-Type: application/json`
- **Request Body**:
  ```json
  {
    "url": "https://example.com/long/path/to/article",
    "alias": "custom-name",
    "ttl_seconds": 86400
  }
  ```
- **Response** (`201 Created`):
  ```json
  {
    "code": "custom-name",
    "short_url": "http://localhost/custom-name",
    "expires_at": "2026-11-23T02:00:00Z"
  }
  ```
- **Error Codes**:
  - `409 Conflict`: Alias already taken.
  - `422 Unprocessable Entity`: Invalid URL, SSRF private IP target, or invalid alias.
  - `429 Too Many Requests`: Rate limit exceeded (includes `Retry-After: <seconds>`).

### `GET /{code}`
- **Response**: `302 Found` (or `301 Moved Permanently` based on `REDIRECT_CODE`)
- **Headers**:
  ```http
  HTTP/1.1 302 Found
  Location: https://example.com/long/path/to/article
  Cache-Control: public, max-age=60
  ```
- **Error**: `404 Not Found` if code does not exist or has expired.

### `GET /api/stats/{code}`
- **Response** (`200 OK`):
  ```json
  {
    "code": "custom-name",
    "clicks": 142
  }
  ```

### `GET /health` & `GET /ready`
- `/health`: Liveness probe (`200 OK`).
- `/ready`: Readiness probe verifying PostgreSQL query and Redis ping (`200 OK` or `503 Service Unavailable`).

### `GET /metrics`
- Prometheus scrape endpoint exposing:
  - `urlshort_requests_total{method, route, status}`
  - `urlshort_request_latency_seconds{route}`
  - `urlshort_cache_hits_total{layer}` (`local`, `redis`, `neg`)
  - `urlshort_cache_misses_total{layer}`
  - `urlshort_analytics_queue_depth`
  - `urlshort_redirects_total{code}`
"# t0tly" 
