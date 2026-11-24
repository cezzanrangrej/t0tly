# Benchmark Suite

This directory contains load generation and telemetry verification scripts for the URL shortener.

## Prerequisites
- Docker & Docker Compose running.
- Stack started: `docker compose up -d`
- Grafana k6 (either installed locally or via Docker `grafana/k6`).

## 1. Seed Database & Codes
Seeds 100,000 URLs into PostgreSQL and pre-warms the top 5,000 keys in Redis:
```bash
docker compose run --rm seeder
```
Or locally:
```bash
python -m bench.seed_db --count 100000 --output bench/codes.txt
```

## 2. Running Load Tests (k6)

### Redirect Hot-Path Test (Zipf Distribution)
Simulates realistic Zipf distributed traffic ($s = 1.07$) against Nginx at 8,000 req/s:
```bash
k6 run --env BASE_URL=http://localhost --env RATE=8000 --env DURATION=60s bench/bench.js
```

### Mixed Read/Write Test
Simulates 99% redirects and 1% link creations:
```bash
k6 run --env BASE_URL=http://localhost bench/mixed_test.js
```

### Full Verification & Matrix Runner
Automates the alternating 6-run matrix (Old vs. New build) with CPU clock and power scheme telemetry:
```bash
python bench/run_matrix.py
```

### Checkpoint Stress & Click Reconciliation Test
Tests queue resiliency and verifies zero click loss under active `CHECKPOINT;` disk contention:
```bash
python bench/run_reconciliation_test.py
```
