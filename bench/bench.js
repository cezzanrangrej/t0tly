/**
 * bench.js — Rigorous bottleneck-investigation script
 *
 * Default: 15s ramp warmup → constant-arrival-rate for DURATION.
 * Set WARMUP_DURATION=0s to skip warmup (cold-start measurement).
 * Logs status + error_code for every non-302/301 response.
 * maxVUs defaults to 5000 so k6 never hits its VU cap up to ~12k req/s.
 *
 * Usage:
 *   k6 run --env BASE_URL=http://192.168.1.37 \
 *           --env RATE=8000 \
 *           --env DURATION=120s \
 *           [--env WARMUP_DURATION=15s] \
 *           [--env PATH_OVERRIDE=/static/x] \
 *           [--env MAX_VUS=5000] \
 *           bench.js
 */

import http from "k6/http";
import { check } from "k6";
import { Rate, Trend, Counter } from "k6/metrics";

// ── Config ────────────────────────────────────────────────────────────────────
const BASE_URL    = __ENV.BASE_URL      || "http://localhost";
const RATE        = Number(__ENV.RATE   || 8000);
const DURATION    = __ENV.DURATION      || "120s";
const MAX_VUS     = Number(__ENV.MAX_VUS || 5000);
// When set, every VU hits this exact path (Exp 2 nginx-static, Exp 3 /bench)
const PATH_OVERRIDE = __ENV.PATH_OVERRIDE || "";

// ── Metrics ───────────────────────────────────────────────────────────────────
const errorRate       = new Rate("error_rate");
const failedRedirects = new Counter("failed_redirects");
const redirectLatency = new Trend("redirect_latency_ms", true);
const statusCounts    = new Counter("redirect_status_count");

// ── Short-code pool (only used when PATH_OVERRIDE is not set) ─────────────────
let codes = [];
if (!PATH_OVERRIDE) {
  const rawCodes = open("./codes.txt");
  codes = rawCodes
    .split(/\r?\n/)
    .map((c) => c.trim())
    .filter((c) => c.length > 0);
  if (codes.length === 0) {
    throw new Error("codes.txt is empty! Run seed_db first.");
  }
}

// Zipf distribution (s = 1.07) — same as redirect_test.js
function getCode() {
  if (PATH_OVERRIDE) return PATH_OVERRIDE;
  if (codes.length === 1) return codes[0];
  const r = Math.random() * 0.9998 + 0.0001;
  const idx = Math.floor(Math.pow(r, -1.0 / 1.07) - 1);
  return "/" + codes[Math.max(0, Math.min(codes.length - 1, idx))];
}

// ── Warmup ────────────────────────────────────────────────────────────────────
// WARMUP_DURATION: ramp from 10% of RATE to RATE before the constant phase.
// Set to "0s" to disable. Default "15s".
const WARMUP_DURATION = __ENV.WARMUP_DURATION !== undefined ? __ENV.WARMUP_DURATION : "15s";
const warmupEnabled   = WARMUP_DURATION !== "0s" && WARMUP_DURATION !== "0";

// ── Options ───────────────────────────────────────────────────────────────────
export const options = {
  systemTags: ["status", "method", "name", "scenario", "expected_response"],
  summaryTrendStats: ["avg", "min", "med", "max", "p(90)", "p(95)", "p(99)"],

  scenarios: {
    bench: warmupEnabled
      ? {
          // Ramp to target rate (primes L1 cache + keepalive connections),
          // then hold constant for the measured DURATION.
          executor:        "ramping-arrival-rate",
          startRate:       Math.max(100, Math.floor(RATE * 0.1)),
          timeUnit:        "1s",
          preAllocatedVUs: Math.min(500, MAX_VUS),
          maxVUs:          MAX_VUS,
          stages: [
            { target: RATE, duration: WARMUP_DURATION },  // warm-up (not measured separately)
            { target: RATE, duration: DURATION },          // MEASURED phase
          ],
        }
      : {
          executor:        "constant-arrival-rate",
          rate:            RATE,
          timeUnit:        "1s",
          duration:        DURATION,
          preAllocatedVUs: Math.min(500, MAX_VUS),
          maxVUs:          MAX_VUS,
        },
  },

  thresholds: {
    // Informational only — do not abort test on threshold breach
    error_rate:          [{ threshold: "rate<0.001",  abortOnFail: false }],
    http_req_duration:   [{ threshold: "p(95)<50",    abortOnFail: false },
                          { threshold: "p(99)<100",   abortOnFail: false }],
    dropped_iterations:  [{ threshold: "count<50",    abortOnFail: false }],
  },
};

// ── VU function ───────────────────────────────────────────────────────────────
let failureLogs = 0;   // shared counter (best-effort, no mutex needed)

export default function () {
  const path = PATH_OVERRIDE ? PATH_OVERRIDE : ("/" + codes[
    (() => {
      const r = Math.random() * 0.9998 + 0.0001;
      const idx = Math.floor(Math.pow(r, -1.0 / 1.07) - 1);
      return Math.max(0, Math.min(codes.length - 1, idx));
    })()
  ]);

  const url = BASE_URL + path;

  const res = http.get(url, {
    redirects: 0,
    tags: { name: "GET " + (PATH_OVERRIDE || "/:code") },
  });

  // ── Status accounting ──────────────────────────────────────────────────────
  statusCounts.add(1, { status: String(res.status) });

  const validStatus   = res.status === 301 || res.status === 302;
  const location      = res.headers["Location"] || res.headers["location"] || "";
  const validLocation = validStatus && Boolean(location);
  const isOk          = validStatus && validLocation;

  if (!isOk) {
    failedRedirects.add(1);
    // Log first 20 failures per VU=1 — captures status AND k6 error_code
    if (__VU === 1 && failureLogs < 20) {
      console.error(
        JSON.stringify({
          t:          new Date().toISOString(),
          url,
          status:     res.status,
          error:      res.error      || "",
          error_code: res.error_code || 0,
          location:   location       || "MISSING",
          body:       String(res.body || "").slice(0, 200),
        })
      );
      failureLogs++;
    }
  }

  check(res, {
    "is 301 or 302":    () => validStatus,
    "has Location":     () => validLocation,
  });

  errorRate.add(!isOk);
  redirectLatency.add(res.timings.duration);
}
