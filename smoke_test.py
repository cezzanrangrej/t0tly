#!/usr/bin/env python3
"""
smoke_test.py — End-to-end verification of the URL shortener stack.
Verifies:
1. Health check (/ready) returns 200 OK.
2. Creating a short link (POST /shorten) returns 201 Created.
3. Fetching the short link (GET /{code}) returns 302 Found with Location header.
4. Analytics background flush records the click in PostgreSQL (/api/stats/{code}).
"""
import sys
import time
import urllib.request
import urllib.error
import json

BASE_URL = "http://localhost"

def run_smoke_test():
    print("=== URL Shortener Smoke Test ===")
    
    # 1. Ready probe
    print("1. Checking service health at /ready...")
    try:
        with urllib.request.urlopen(f"{BASE_URL}/ready", timeout=5) as resp:
            if resp.status != 200:
                print(f"[FAIL] /ready returned status {resp.status}")
                sys.exit(1)
            data = json.loads(resp.read().decode())
            print(f"   [✓] System healthy: db={data.get('db')}, redis={data.get('redis')}")
    except Exception as e:
        print(f"[FAIL] Unable to connect to {BASE_URL}/ready: {e}")
        sys.exit(1)

    # 2. Shorten URL
    target_url = "https://example.com/smoke-test-target"
    print(f"2. Creating short link for '{target_url}'...")
    payload = json.dumps({"url": target_url}).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/shorten",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 201:
                print(f"[FAIL] POST /shorten returned status {resp.status}")
                sys.exit(1)
            created = json.loads(resp.read().decode())
            code = created["code"]
            print(f"   [✓] Short link created: {created['short_url']} (code: {code})")
    except Exception as e:
        print(f"[FAIL] Shorten request failed: {e}")
        sys.exit(1)

    # 3. Follow redirect (without following to destination)
    print(f"3. Requesting GET /{code} (redirect check)...")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        req = urllib.request.Request(f"{BASE_URL}/{code}", headers={"User-Agent": "SmokeTest/1.0"})
        resp = opener.open(req, timeout=5)
        status = resp.status
        location = resp.headers.get("Location")
    except urllib.error.HTTPError as e:
        status = e.code
        location = e.headers.get("Location")
    except Exception as e:
        print(f"[FAIL] Redirect request failed: {e}")
        sys.exit(1)

    if status not in (301, 302):
        print(f"[FAIL] Expected HTTP 301/302, got status {status}")
        sys.exit(1)
    if location != target_url:
        print(f"[FAIL] Location header '{location}' does not match target '{target_url}'")
        sys.exit(1)
    print(f"   [✓] Redirect successful: HTTP {status} -> {location}")

    # 4. Analytics flush check
    print("4. Waiting 1.5s for async analytics queue flush...")
    time.sleep(1.5)

    stats_url = f"{BASE_URL}/api/stats/{code}"
    print(f"   Checking click count at {stats_url}...")
    try:
        with urllib.request.urlopen(stats_url, timeout=5) as resp:
            if resp.status != 200:
                print(f"[FAIL] GET /api/stats/{code} returned status {resp.status}")
                sys.exit(1)
            stats = json.loads(resp.read().decode())
            clicks = stats.get("clicks", 0)
            if clicks < 1:
                print(f"[FAIL] Expected clicks >= 1, got {clicks}")
                sys.exit(1)
            print(f"   [✓] Analytics verified: {clicks} click(s) recorded in database.")
    except Exception as e:
        print(f"[FAIL] Stats request failed: {e}")
        sys.exit(1)

    print("\n[SUCCESS] All smoke test checks passed cleanly!")

if __name__ == "__main__":
    run_smoke_test()
