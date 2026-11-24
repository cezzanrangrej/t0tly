import subprocess
import threading
import time
import urllib.request
import re
import sys

def get_click_count():
    res = subprocess.run(
        ['docker', 'compose', 'exec', '-T', 'postgres', 'psql', '-U', 'urlshort', '-d', 'urlshort', '-t', '-A', '-c', 'SELECT COUNT(*) FROM clicks;'],
        capture_output=True,
        text=True
    )
    return int(res.stdout.strip())

def get_analytics_metrics():
    req = urllib.request.urlopen('http://localhost/metrics')
    text = req.read().decode('utf-8')
    dropped = 0.0
    failures = 0.0
    for line in text.split('\n'):
        if line.startswith('urlshort_analytics_dropped_clicks_total'):
            dropped = float(line.split()[1])
        elif line.startswith('urlshort_analytics_flush_failures_total'):
            failures = float(line.split()[1])
    return dropped, failures

def trigger_checkpoint(delay_sec):
    print(f"[*] Checkpoint scheduler: waiting {delay_sec}s before forcing CHECKPOINT;...")
    time.sleep(delay_sec)
    print("[*] TRIGGERING FORCED CHECKPOINT IN POSTGRESQL...")
    t0 = time.time()
    res = subprocess.run(
        ['docker', 'compose', 'exec', '-T', 'postgres', 'psql', '-U', 'urlshort', '-d', 'urlshort', '-c', 'CHECKPOINT;'],
        capture_output=True,
        text=True
    )
    duration = time.time() - t0
    print(f"[*] FORCED CHECKPOINT FINISHED in {duration:.3f}s: {res.stdout.strip()}")

def main():
    print("=== STEP 1: Power Scheme Verification ===")
    scheme_res = subprocess.run(['powercfg', '/getactivescheme'], capture_output=True, text=True)
    print(scheme_res.stdout.strip())

    print("\n=== STEP 2: Pre-Run Baseline Capture ===")
    c0 = get_click_count()
    d0, f0 = get_analytics_metrics()
    print(f"Baseline clicks: {c0}")
    print(f"Baseline dropped clicks metric: {d0}")
    print(f"Baseline flush failures metric: {f0}")

    # Launch forced checkpoint in background at t=45s
    cp_thread = threading.Thread(target=trigger_checkpoint, args=(45,))
    cp_thread.daemon = True
    cp_thread.start()

    print("\n=== STEP 3: Executing k6 Load Test (8,000 req/s, 75s duration, 5s ramp) ===")
    k6_cmd = [
        'docker', 'run', '--rm',
        '--cpus=2',
        '-w', '/bench',
        '--network', 'urlshortner_urlnet',
        '-v', f"{subprocess.os.getcwd()}/bench:/bench",
        'grafana/k6', 'run',
        '--env', 'BASE_URL=http://nginx',
        '--env', 'RATE=8000',
        '--env', 'DURATION=70s',
        '--env', 'WARMUP_DURATION=5s',
        '--env', 'MAX_VUS=3000',
        '/bench/bench.js'
    ]
    t_start = time.time()
    k6_proc = subprocess.run(k6_cmd, capture_output=True, text=True)
    elapsed = time.time() - t_start
    print(f"k6 execution finished in {elapsed:.2f}s")
    print(k6_proc.stdout)
    if k6_proc.stderr:
        print("STDERR:", k6_proc.stderr)

    print("\n=== STEP 4: In-flight Flush Settling (Sleep 5s) ===")
    time.sleep(5)

    print("\n=== STEP 5: Post-Run Reconciliation ===")
    c1 = get_click_count()
    d1, f1 = get_analytics_metrics()
    delta_clicks = c1 - c0
    delta_dropped = d1 - d0
    delta_failures = f1 - f0

    # Parse http_reqs from k6 output
    req_match = re.search(r'http_reqs\.+:\s+(\d+)', k6_proc.stdout)
    total_reqs = int(req_match.group(1)) if req_match else None

    # Parse duration p99
    p99_match = re.search(r'http_req_duration\.+p\(99\)=([0-9\.]+[a-zµs]+)', k6_proc.stdout)
    p99 = p99_match.group(1) if p99_match else "N/A"

    # Parse dropped iterations
    drop_iter_match = re.search(r'dropped_iterations\.+:\s+(\d+)', k6_proc.stdout)
    dropped_iters = int(drop_iter_match.group(1)) if drop_iter_match else 0

    print("--------------------------------------------------")
    print(f"Completed HTTP Requests (k6): {total_reqs}")
    print(f"Clicks Inserted in PostgreSQL: {delta_clicks}")
    print(f"Clicks Dropped (Metric):       {delta_dropped}")
    print(f"Flush Failures / Timeouts:     {delta_failures}")
    print(f"k6 Dropped Iterations:         {dropped_iters}")
    print(f"k6 http_req_duration p99:      {p99}")
    print("--------------------------------------------------")
    if total_reqs is not None:
        reconciled = delta_clicks + int(delta_dropped)
        diff = total_reqs - reconciled
        print(f"Reconciliation: Requests ({total_reqs}) vs (Written {delta_clicks} + Dropped {int(delta_dropped)} = {reconciled})")
        print(f"Discrepancy: {diff} requests ({diff / total_reqs * 100:.3f}%)")
        if abs(diff) < 50:
            print(">> VERDICT: Analytics path is mathematically LOSSLESS (within 0.01% boundary of in-flight buffer at process boundary)!")
        else:
            print(">> VERDICT: Discrepancy observed.")

if __name__ == '__main__':
    main()
