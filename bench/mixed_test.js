// k6 mixed load test: 4,950 redirects/s + 50 creates/s
// Usage: k6 run --env BASE_URL=http://127.0.0.1:8000 loadtest/mixed_test.js

import http from "k6/http";
import { check } from "k6";
import { Rate, Trend } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://127.0.0.1:8000";
const errorRate = new Rate("error_rate");
const redirectLatency = new Trend("redirect_latency_ms", true);
const createLatency = new Trend("create_latency_ms", true);

// Load pre-seeded codes for redirects
const rawCodes = open("./codes.txt");
const codes = rawCodes.split("\n").map(c => c.trim()).filter(c => c.length > 0);

const SEED_URLS = [
  "https://example.com/page1",
  "https://example.com/page2",
  "https://github.com/features",
  "https://docs.python.org/3/",
  "https://fastapi.tiangolo.com/",
  "https://redis.io/docs/",
  "https://www.postgresql.org/docs/",
  "https://nginx.org/en/docs/",
  "https://prometheus.io/docs/",
  "https://grafana.com/docs/",
];

function zipf(n, s) {
  if (n <= 0) return 0;
  const r = Math.random() + 0.00001;
  return Math.min(n - 1, Math.floor(Math.pow(r, -1.0 / s) - 1));
}

export const options = {
  scenarios: {
    redirects: {
      executor: "ramping-arrival-rate",
      startRate: 500,
      timeUnit: "1s",
      preAllocatedVUs: 100,
      maxVUs: 200,
      stages: [
        { target: 4950, duration: "10s" },
        { target: 4950, duration: "30s" },
      ],
      exec: "doRedirect",
    },
    creates: {
      executor: "constant-arrival-rate",
      rate: 50,
      timeUnit: "1s",
      duration: "40s",
      preAllocatedVUs: 10,
      maxVUs: 50,
      exec: "doCreate",
    },
  },
  thresholds: {
    "redirect_latency_ms": ["p(99)<50"],
    "http_req_duration{scenario:redirects}": ["p(99)<50"],
    error_rate: ["rate<0.001"],
  },
};

export function doRedirect() {
  const idx = zipf(codes.length, 1.07);
  const code = codes[idx] || codes[0];

  const res = http.get(`${BASE_URL}/${code}`, {
    redirects: 0,
    tags: { scenario: "redirects" },
  });

  const ok = check(res, {
    "redirect ok": (r) => r.status === 301 || r.status === 302,
    "has Location": (r) => !!r.headers["Location"],
  });
  errorRate.add(!ok);
  redirectLatency.add(res.timings.duration);
}

export function doCreate() {
  const url = SEED_URLS[Math.floor(Math.random() * SEED_URLS.length)];
  const payload = JSON.stringify({
    url: url + "?r=" + Math.random().toString(36).slice(2),
  });

  // Unique client IP to simulate distributed callers within rate limits
  const clientIp = `198.51.${Math.floor(Math.random() * 250)}.${Math.floor(Math.random() * 250)}`;

  const res = http.post(`${BASE_URL}/shorten`, payload, {
    headers: {
      "Content-Type": "application/json",
      "X-Real-IP": clientIp,
    },
    tags: { scenario: "creates" },
  });

  const ok = check(res, {
    "created": (r) => r.status === 201,
  });
  errorRate.add(!ok);
  createLatency.add(res.timings.duration);
}
