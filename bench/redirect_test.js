import http from "k6/http";
import { check } from "k6";
import { Rate, Trend, Counter } from "k6/metrics";

// Route traffic through Nginx by default (port 80).
// Override with --env BASE_URL=http://localhost if needed.
const BASE_URL = __ENV.BASE_URL || "http://localhost";

// Configurable arrival rate, test duration, and warmup.
const RATE = Number(__ENV.RATE || 6000);
const DURATION = __ENV.DURATION || "30s";
const WARMUP = __ENV.WARMUP !== undefined ? __ENV.WARMUP : "3s";

// Auto-size VUs based on target arrival rate if not overridden
const PREALLOCATED_VUS = Number(
  __ENV.PREALLOCATED_VUS || Math.max(50, Math.min(400, Math.floor(RATE / 15)))
);
const MAX_VUS = Number(
  __ENV.MAX_VUS || Math.max(100, Math.min(800, Math.floor(RATE / 6)))
);

// Traffic distribution: "zipf" (default, models real-world cache hit ratios) or "uniform"
const DISTRIBUTION = (__ENV.DISTRIBUTION || "zipf").toLowerCase();

// Custom metrics
const errorRate = new Rate("error_rate");
const failedRedirects = new Counter("failed_redirects");
const redirectLatency = new Trend("redirect_latency_ms", true);
const statusCounts = new Counter("redirect_status_count");

// Load pre-seeded short codes
const rawCodes = open("./codes.txt");
const codes = rawCodes
  .split(/\r?\n/)
  .map((code) => code.trim())
  .filter((code) => code.length > 0);

if (codes.length === 0) {
  throw new Error("codes.txt is empty or contains no valid short codes! Run seed_db first.");
}

// Low-cardinality Zipf generator
function getCodeIndex(totalCodes) {
  if (DISTRIBUTION === "uniform" || totalCodes <= 1) {
    return Math.floor(Math.random() * totalCodes);
  }
  // Zipf distribution (exponent s = 1.07):
  // Safe bounded clamp ensuring index is strictly in [0, totalCodes - 1]
  const r = Math.random() * 0.9998 + 0.0001;
  const rawIdx = Math.floor(Math.pow(r, -1.0 / 1.07) - 1);
  return Math.max(0, Math.min(totalCodes - 1, rawIdx));
}

let failureLogs = 0;

export const options = {
  // Low-cardinality system tags
  systemTags: [
    "status",
    "method",
    "name",
    "scenario",
    "expected_response"
  ],

  summaryTrendStats: ["avg", "min", "med", "max", "p(90)", "p(95)", "p(99)"],

  scenarios: {
    redirects:
      WARMUP !== "0s" && WARMUP !== "0"
        ? {
          executor: "ramping-arrival-rate",
          startRate: Math.min(500, RATE),
          timeUnit: "1s",
          preAllocatedVUs: PREALLOCATED_VUS,
          maxVUs: MAX_VUS,
          stages: [
            { target: RATE, duration: WARMUP },     // Warm-up ramp to prime TCP & cache
            { target: RATE, duration: DURATION },   // Sustained benchmark
          ],
        }
        : {
          executor: "constant-arrival-rate",
          rate: RATE,
          timeUnit: "1s",
          duration: DURATION,
          preAllocatedVUs: PREALLOCATED_VUS,
          maxVUs: MAX_VUS,
        },
  },

  thresholds: {
    // Fail if error rate exceeds 0.1%
    error_rate: ["rate<0.001"],
    // Target p95 < 50ms, p99 < 100ms
    http_req_duration: ["p(95)<50", "p(99)<100"],
    // Dropped iterations threshold
    dropped_iterations: ["count<50"],
  },
};

export default function () {
  const idx = getCodeIndex(codes.length);
  const code = codes[idx];
  const url = `${BASE_URL}/${code}`;

  const res = http.get(url, {
    // CRITICAL: redirects: 0 prevents k6 from following the 301/302 response
    // and measures the shortener service's redirect latency directly.
    redirects: 0,
    tags: {
      route: "redirect",
      name: "GET /:code",
    },
  });

  // Count HTTP response status codes
  statusCounts.add(1, {
    status: String(res.status),
  });

  // Validate redirect status and Location header
  const validStatus = res.status === 301 || res.status === 302;
  const location = res.headers["Location"] || res.headers["location"] || "";
  const validLocation = Boolean(location);
  const isOk = validStatus && validLocation;

  // Track failures separately
  if (!isOk) {
    failedRedirects.add(1);
    if (__VU === 1 && failureLogs < 10) {
      console.error(
        `[REDIRECT FAILURE] status: ${res.status} | url: ${url} | location: ${location || "MISSING"} | body: ${String(res.body || "").slice(0, 150)}`
      );
      failureLogs++;
    }
  }

  // k6 check assertions (note: checks metric is checks/sec, not requests/sec)
  const ok = check(res, {
    "is 301 or 302": () => validStatus,
    "has Location header": () => validLocation,
  });

  // Record error rate and latency trend
  errorRate.add(!ok);
  redirectLatency.add(res.timings.duration);
}
