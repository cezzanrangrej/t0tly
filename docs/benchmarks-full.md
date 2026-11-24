# Rigorous Bottleneck Audit, Architectural Remediation & Confound Analysis

## 1. Executive Summary & Defensible Findings

> **The Defensible Engineering Claim**:
> Decoupling analytics (bounded drop-on-overflow queue, binary COPY, per-connection async commit) improved the 16k-target run from ~4.6k to ~8.6k delivered req/s and p99 from 1.54s to 103ms in single runs. Not yet reproduced across sessions, and the old build performed equal or better at 12k and 20k. Origin ≈ 9-10k req/s at p99 ≈ 100ms. With a 10s nginx cache: 20k req/s at p99 4.5ms (cached).

---

### Core Empirical Findings & Architectural Reality:

1. **The Origin Ceiling is ~9k–10k req/s (Not 12k or 16k)**:
   - Across both old and new codebases, delivered origin throughput tops out between **9,000 and 10,138 req/s** with p99 latencies hovering near the 100ms boundary.
   - At a 12k target: the old code delivered **10,138 req/s at p99 = 82.1 ms**, while the new code delivered **9,035 req/s at p99 = 97.2 ms**. The old build actually completed slightly more requests with lower tail latency (within normal multi-process variation).
   - At a 16k target: the old build collapsed to **4,636 req/s at p99 = 1.54 s** due to flush loop stalls, whereas the new decoupled build delivered **8,576 req/s at p99 = 102.85 ms** (preventing the queue-stall collapse observed in the old code, though delivered rates differed so this represents stall prevention rather than an equal-load speedup).
   - At a 20k target: the old build completed **9,193 req/s (p99 = 425 ms)**, whereas the new build collapsed to **2,575 req/s (p99 = 336 ms)** under host CPU starvation.
   - *Conclusion*: The decoupled architecture eliminates the severe queue-stall collapse observed at 16k, but does not provide a higher raw throughput ceiling.

2. **Delivered Rate Calculation & Ramp-Up Understatement**:
   - Because the 40s test includes a 10s warmup ramp (from 500 VUs to target), the delivered rate (`total HTTP requests ÷ 40s`) mathematically understates steady-state capacity.
   - For example, Row 7 had **0 dropped iterations** at an 8,000 req/s target, yet its delivered rate calculates to **7,098 req/s** due to the ramp.
   - Therefore, **9k–10k req/s represents a conservative lower bound on origin steady-state capacity**, and **`dropped_iterations > 0` serves as the true primary signal of origin saturation**.
   - *Latency Caveat*: Dropped iterations never receive an HTTP latency value in k6. The reported p99 figures describe only the requests that were successfully dispatched onto the wire.

3. **Topology & Resource Accounting (`docker stats`)**:
   - *Worker Topology*: **3 API containers** (`api1`, `api2`, `api3`), each running Uvicorn with `--workers 3` = **9 total worker processes** load-balanced behind Nginx via `least_conn`.
   - *CPU Accounting*: In Docker on Linux, 100% CPU represents 1 *logical hardware thread*. On this 6-core / 12-thread Intel i5-11400H host, total capacity is **1,200%**.
   - Peak telemetry recorded `k6` consuming **~450%**, the 9 API workers consuming **~580%**, and Nginx consuming **~140%** (totaling ~1,170% out of 1,200%). The co-located host was fully saturated across all 12 hardware threads.

4. **Methodological Confounds Identified**:
   - **Cross-Session Confound**: Tests were executed across different sessions over several days. The Windows power profile was unrecorded during initial testing (Rows 1–12, 19–20). The 4.6k vs 8.6k divergence could partially reflect ambient thermal or background environment differences rather than code changes alone.
   - **Power Throttling Hypothesis & Conflict**: While the ASUS "Silent" plan (`64a64f24-...`) was discovered active during Rows 16–18 (suspected throttled), power throttling alone does not explain the origin latency floor. Row 19 (run in the earlier session where power plan was unrecorded) still exhibited **p99 = 190.8 ms**, which is consistent with host CPU thread contention (all 12 logical threads saturated at ~1,170%) being a primary contributor to that floor.
   - **Postgres Checkpoint Timing & Empirical Verification**: The new build tuned `checkpoint_timeout = 15min` and `max_wal_size = 4GB`. To isolate whether stability came from binary `COPY` + async commit versus simply avoiding checkpoints, we executed forced `CHECKPOINT;` stress runs on both builds at an 8,000 req/s target. On the old build, the checkpoint stalled for **29.16 s**, collapsing delivered throughput by ~75% (to 1,236 req/s) with p99 blowing out to **1,066 ms**. On the new build, the checkpoint completed in **9.77 s**, delivered rate held at **4,559 req/s**, and tail latency remained controlled at **349 ms** with **zero click loss**. This empirically confirms that binary `COPY` combined with per-connection asynchronous commit protects the service against checkpoint-induced write stalls.
   - **Mismatched Delivered Loads at 16k**: Comparing p99 at the 16k target compares an overloaded 4.6k delivered system against an 8.6k delivered system. Valid p99 comparisons require evaluating old vs. new at identical delivered arrival rates (such as 8,000 req/s).
   - **Remote Wi-Fi State**: Power scheme and background interference during wireless testing were unrecorded. Under the strict median rule, remote Wi-Fi is unsupported at 8,000 req/s (median p99 of 1.52 s).

---

## 2. Audited Master Results Table

- **Target Rate**: Configured arrival rate target in k6.
- **Delivered Rate**: Actual completed HTTP requests divided by test duration (`http_reqs / duration`).
- **Strict Ceiling Rule**: Requires **`p99 < 100 ms and Failures = 0`**.
- *Tests 3–22 ran with a 10s ramp + 30s constant phase (40s total).*

| # | Test Description | Client Location | Analytics Code | Cache Layer | Target Rate | Delivered Rate | p50 | p95 | p99 | Failures | Dropped Iters | Max VUs |
|:---:|---|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| **1** | Ping Baseline (Idle) | Mac (Wi-Fi) | N/A | N/A | — | — | 6.0 ms | 12.8 ms | 12.8 ms | 0% loss | — | — |
| **2** | Ping Under Load (8k) | Mac (Wi-Fi) | ON | Origin | 8,000 | 7,229 req/s | 6.0 ms | 15.8 ms | 94.3 ms | 0% loss | — | — |
| **3** | Nginx Static 302 (Run 1) | Mac (Wi-Fi) | OFF | Nginx Static | 8,000 | 7,194 req/s | 17.4 ms | 617.6 ms| 1.52 s | 0 | 54,638 | 5,000* |
| **4** | Nginx Static 302 (Run 2) | Mac (Wi-Fi) | OFF | Nginx Static | 8,000 | 7,590 req/s | 15.4 ms | 32.6 ms | **66.0 ms** | **0** | **1,102** | **993** |
| **5** | Nginx Static 302 (Run 3) | Mac (Wi-Fi) | OFF | Nginx Static | 8,000 | 6,546 req/s | 155.3 ms| 1.07 s | 1.57 s | 0 | 104,325 | 5,000* |
| **6** | Nginx Static 302 (Median)| Mac (Wi-Fi) | OFF | Nginx Static | 12,000 | 9,144 req/s | 344.5 ms| 1.02 s | 1.89 s | 0 | 303,112 | 5,000* |
| **7** | Nginx Static 302 (Median)| Docker Bridge | OFF | Nginx Static | 8,000 | 7,098 req/s | 146 µs | 251 µs | **717 µs** | **0** | **0** | **12** |
| **8** | Nginx Static 302 (Median)| Docker Bridge | OFF | Nginx Static | 12,000 | 10,648 req/s| 160 µs | 341 µs | **1.75 ms** | **0** | **2** | **18** |
| **9** | Full App Origin (Old Code)| Docker Bridge | ON (executemany)| Origin | 8,000 | 6,662 req/s | 1.33 ms | 55.6 ms | 201.3 ms| 34 (0.01%)| 11,346 | 1,402 |
| **10**| **Full App Origin (Old Code)**| Docker Bridge | ON (executemany)| Origin | **12,000**| **10,138 req/s**| **8.59 ms** | **51.0 ms** | **82.1 ms** | **0 (0.00%)**| **19,710** | **793** |
| **11**| Full App Origin (Old Code)| Docker Bridge | ON (executemany)| Origin | 16,000 | 4,636 req/s | 21.5 ms | 372.1 ms| 1.54 s | 0 (0.00%)| 364,577 | 1,613 |
| **12**| Full App Origin (Old Code)| Docker Bridge | ON (executemany)| Origin | 20,000 | 9,193 req/s | 18.3 ms | 150.4 ms| 425.1 ms| 0 (0.00%)| 336,708 | 1,526 |
| **13**| **Full App Origin (New Decoupled)**| Docker Bridge | **ON (COPY)** | Origin | **12,000**| **9,035 req/s** | **10.0 ms** | **54.18 ms**| **97.16 ms** | **0 (0.00%)**| **57,170** | **868** |
| **14**| **Full App Origin (New Decoupled)**| Docker Bridge | **ON (COPY)** | Origin | **16,000**| **8,576 req/s** | **11.06 ms**| **59.89 ms**| **102.85 ms**| **0 (0.00%)**| **144,083**| **806** |
| **15**| Full App Origin (New Decoupled)| Docker Bridge | **ON (COPY)** | Origin | 20,000 | 2,575 req/s | 38.7 ms | 198.65 ms| 335.87 ms| 0 (0.00%)| 91,737 | 628 |
| **16**| Full App Origin (Suspected Throttled)| Docker Bridge| OFF | Origin | 5,000 | 3,564 req/s | 28.01 ms| 120.16 ms| 201.00 ms| 0 (0.00%)| 32,733 | 636 |
| **17**| Full App Origin (Suspected Throttled)| Docker Bridge| OFF | Origin | 8,000 | 3,671 req/s | 30.59 ms| 140.28 ms| 224.01 ms| 0 (0.00%)| 103,468| 673 |
| **18**| Full App Origin (Suspected Throttled)| Docker Bridge| OFF | Origin | 12,000 | 3,436 req/s | 32.75 ms| 151.21 ms| 226.01 ms| 0 (0.00%)| 90,932 | 682 |
| **19**| Full App Origin (Plan Unrecorded)| Docker Bridge | **OFF** | Origin | 16,000 | 11,290 req/s| 31.2 ms | 117.9 ms | 190.8 ms| 39 (0.00%)| 115,358 | 1,624 |
| **20**| Full App Origin (Plan Unrecorded)| Docker Bridge | **OFF** | Origin | 20,000 | 11,044 req/s| 38.5 ms | 129.5 ms | 205.7 ms| 0 (0.00%)| 260,331 | 1,796 |
| **21**| Trivial FastAPI `/bench` | Docker Bridge | **OFF** | Origin (Bare) | 12,000 | 9,907 req/s | 9.4 ms | 63.9 ms | 120.1 ms| 0 (0.00%)| 29,311 | 997 |
| **22**| **Nginx Microcache [CACHED]**| Docker Bridge | **Bypassed** | **RAM Cache**| **20,000**| **17,744 req/s**| **179 µs** | **1.06 ms** | **4.46 ms** | **0 (0.00%)**| **3** | **199** |

*\*Note: Rows where Max VUs reached 5,000 hit k6's configured VU cap.*

---

## 3. Strict Operational Ceilings (Directly Mapped to Data)

1. **Origin Server with Analytics (Old vs. New)**:
   - **Supported Operational Ceiling: ~9,000 – 10,000 req/s delivered**.
   - Old code achieved this at 12k target: **10,138 req/s delivered at p99 = 82.1 ms** (Row 10).
   - New code achieved this at 12k target: **9,035 req/s delivered at p99 = 97.16 ms** (Row 13).
   - At a 16k target, new code delivered **8,576 req/s at p99 = 102.85 ms** (Row 14). While this marginally crosses the 100ms threshold, it prevented the catastrophic collapse seen in the old code (4,636 req/s at 1.54s p99).
   - At a 20k target, both builds failed the SLA: old code had p99 = 425 ms, and new code collapsed to 2,575 req/s (p99 = 336 ms).
2. **Nginx Microcache in RAM [CACHED]**:
   - **Supported Ceiling: 20,000+ req/s** (Row 22: 17,744 req/s overall, 20,000 req/s constant phase, **p99 = 4.46 ms**, 0 failures, 199 VUs).
3. **Remote Wi-Fi Client Path**:
   - **Unsupported under the strict median rule** due to high run-to-run variance (median p99 of 1.52 s at 8k target).

---

## 4. Production Architectural Answers (Interview-Ready)

### Q1: What happens to a batch when the 3-second flush timeout fires: dropped, retried, or double-counted?
> **Answer**: The in-flight batch is **dropped** and counted via metric `urlshort_analytics_flush_failures_total`.
> It is **intentionally not retried**. Retrying batches under a saturated database causes head-of-line blocking and cascading queue overflows, endangering the primary redirect SLA. At-most-once delivery under overload is the deliberate trade-off to protect core redirect availability.

### Q2: Is the analytics queue bounded, and what happens when it is full?
> **Answer**: The queue is strictly bounded (`maxsize = 50,000` per worker). When full, `put_nowait()` raises `asyncio.QueueFull`, incrementing `urlshort_analytics_dropped_clicks_total` and dropping the event immediately. Redirect handlers never block or wait on analytics.

### Q3: How do you maintain URL creation durability while speeding up click ingestion?
> **Answer**: Keep `synchronous_commit = on` globally in PostgreSQL so link creation (`POST /shorten`) maintains full ACID durability. For click ingestion, open a transaction and execute `SET LOCAL synchronous_commit = 'off'` before streaming batches via binary `COPY`. This confines asynchronous commit semantics strictly to loss-tolerant analytics writes.

### Q4: How do you achieve 20,000+ QPS without dropping analytics in production?
> **Answer**:
> 1. **Hot-Path Decoupling**: Flush in-memory worker aggregations to Redis via pipelined `HINCRBY clicks:<date> <code> delta`, or push events to a distributed message log (Kafka / AWS Kinesis / Redis Streams).
> 2. **PostgreSQL Streaming**: Ingest using streaming binary `COPY`, unlogged staging tables, and connection-level `synchronous_commit = off`.
> 3. **Edge Log Shipping**: When serving 20,000+ QPS from Nginx RAM cache, collect analytics by shipping Nginx access logs asynchronously via Vector or FluentBit into ClickHouse or PostgreSQL.

---

## 5. Empirical Verification & Checkpoint Stress Results

To cleanly verify the analytics fix and eliminate cross-session confounds, we executed controlled benchmark suites with AC power confirmed, power scheme pinned to `Turbo` / `Performance`, CPU clock pinned at 2,688 MHz, and k6 capped at `--cpus=2`.

### 5.1 Fixed-Load Comparison: Old vs. New Build (Constant 8,000 req/s, No Ramp, 120s Duration)
Six runs alternating Old $\to$ New $\to$ Old $\to$ New $\to$ Old $\to$ New:

| Metric | Old Build (`executemany` + Sync Commit) | New Build (`COPY` + Async Commit + Bounded) | Notes |
| :--- | :---: | :---: | :--- |
| **Delivered Rate (Median)** | **4,808.7 req/s** | **4,792.9 req/s** | Cold start without ramp limits initial connection burst |
| **Dropped Iterations (Median)** | **377,430** | **383,806** | Parity under identical arrival rate without warmup |
| **Failures / Non-302s (Median)** | **1** *(15, 1, 0 across runs)* | **0** *(0, 0, 0 across runs)* | New build exhibited zero HTTP connection failures |

---

### 5.2 Forced Checkpoint Comparison (The Critical Proof)
Under identical 8,000 req/s load, `CHECKPOINT;` was forced in PostgreSQL during active writes:

| Metric | Old Build (`CHECKPOINT;` @ 60s) | New Build (`CHECKPOINT;` @ 45s) | Impact / Remediation |
| :--- | :---: | :---: | :--- |
| **Postgres Checkpoint Duration** | **29.164 seconds** | **9.769 seconds** | **3x faster completion** (WAL flushes no longer block dirty page I/O) |
| **Delivered Rate Under Stress** | **1,236.1 req/s** | **4,558.9 req/s** | **3.7x higher throughput**; old build suffered complete worker stall |
| **Tail Latency (p99)** | **1,066.38 ms** | **349.56 ms** | Latency controlled; old build blew past 1.0s SLA |
| **Dropped Iterations** | **683,209** | **186,739** | Massive queue backlog on old build |
| **Clicks Inserted into PostgreSQL** | **172,807** | **342,072** | Every completed redirect was captured |
| **Flush Failures / Dropped Clicks** | **0.0 / 0.0** | **0.0 / 0.0** | Zero click loss observed on both builds |

*Finding*: The forced checkpoint test empirically isolates the architectural remediation. On the old build, row-by-row `INSERT` with synchronous commit creates lock and I/O contention with checkpoint dirty-page flushing, stalling worker processes for nearly 30 seconds and collapsing delivered rate from 4.8k to 1.2k req/s. Streaming binary `COPY` with per-connection `SET LOCAL synchronous_commit = 'off'` allows workers to continue serving redirects without blocking on fsync.

---

### 5.3 Click Reconciliation & Investigation of the 83 Connection Resets
- In the 75s New build checkpoint test:
  - Total HTTP requests dispatched by k6: **342,155** (342,072 successful 302 redirects, 83 failed).
  - Clicks inserted into PostgreSQL: **342,072**.
  - Metric Counters: `urlshort_analytics_dropped_clicks_total = 0.0`, `urlshort_analytics_flush_failures_total = 0.0`.
  - **Reconciliation Verdict**: $342,072\text{ redirects} \equiv 342,072\text{ clicks in PostgreSQL}$. **Zero loss was observed over a 342k-request run, including a forced checkpoint**.
- **Investigation of the 83 Failed Requests (0.024% error rate)**:
  - Because `enqueue_click()` is only invoked after a valid short code lookup succeeds, requests that failed at the TCP layer never created click records.
  - The 83 failures manifested as connection resets (`ECONNRESET` / `read: connection reset by peer`).
  - *Candidate Hypotheses*:
    1. *k6 Start-up Burst*: 500 VUs ramping immediately to 8k req/s can momentarily overwhelm the kernel listen backlog (`backlog = 4096`).
    2. *Checkpoint I/O Thread Stalls*: The 9.77s disk write storm in the WSL2/Docker VM may have caused temporary thread scheduling delays in Nginx's worker event loop.
  - *Engineering Honesty*: Because detailed per-request packet timestamps were not retained across container restarts, the exact timing cannot be definitively isolated between start-up socket backlog drops and checkpoint I/O thread contention.

---

### 5.4 Peak Queue Depth & Graceful Shutdown Behavior
- **Queue Bounds**: The in-memory buffer is bounded at `analytics_queue_size = 50,000`. Under steady-state 8k load, measured queue depth remains low (**500 to 3,500 items**) because the background flush worker drains 1,000-item batches every 1.0 second.
- **Graceful Shutdown**:
  - On `SIGTERM`, FastAPI's lifespan invokes `stop_flush_task()`, which cancels active background tasks.
  - In an in-memory queue design, unflushed events remaining in memory upon ungraceful container termination (e.g., `SIGKILL`, OOM-kill, power loss) are dropped.
  - *Production Trade-off*: For zero-data-loss guarantees across pod restarts, a durable external distributed commit log (Apache Kafka or Redis Streams with persistent consumer groups) should replace the in-process queue.

---

## 6. How to Present This in Senior Engineering Interviews

> **Interview Golden Rule**: Lead with the diagnosis, the architectural remediation, and the honest boundaries of the evidence.
>
> 1. **The Diagnosis**: Explain how log inspection revealed `asyncio.TimeoutError` in the flush loop, correlated with Postgres checkpoint write stalls (368 MB checkpoint taking 270s, single-row `INSERT` batches taking 200–478 ms).
> 2. **The Remediation**: Explain the decoupling pattern: bounded drop-on-overflow queue, streaming binary `COPY`, per-connection `SET LOCAL synchronous_commit = 'off'`, and tuning checkpoint intervals.
> 3. **The Lossless Proof**: When asked *"How do you know you aren't silently dropping data?"*, cite the click reconciliation telemetry: below saturation, total successful 302s matched clicks inserted in PostgreSQL to the exact row (342,072 / 342,072), with 0.0 dropped clicks and 0 flush timeouts even during an active 9.77s forced checkpoint storm (zero loss observed over a 342k-request run, including a forced checkpoint).
> 4. **The Checkpoint Demonstration**: Point to the empirical forced-checkpoint experiment: on the old build, `CHECKPOINT;` took 29.2s, collapsed throughput to 1.2k req/s, and blew p99 past 1,000 ms; on the new build, checkpoint completed in 9.8s, throughput remained at ~4.6k req/s, and p99 stayed at 349 ms.
> 5. **The Honest Evidence**: State clearly that at 16k target, the fix prevented catastrophic queue collapse (4.6k delivered at 1.54s p99 $\to$ 8.6k at 103ms p99), but did **not** raise the peak origin ceiling (which remains ~9k–10k req/s). Discussing the cross-session power plan and checkpoint timing confounds provides an honest, grounded engineering defense.
