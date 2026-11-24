import os
import sys
import json
import time
import re
import subprocess
import threading
import urllib.request

LOG_FILE = "benchmark_telemetry.log"
WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def log_telemetry(msg: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] {msg}\n"
    print(entry, end="")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(entry)

def get_system_telemetry():
    try:
        scheme_res = subprocess.run(["powercfg", "/getactivescheme"], capture_output=True, text=True)
        scheme = scheme_res.stdout.strip()
    except Exception as e:
        scheme = f"Error: {e}"
    
    try:
        proc_res = subprocess.run(
            ["powershell", "-Command", "Get-CimInstance Win32_Processor | Select-Object -ExpandProperty CurrentClockSpeed"],
            capture_output=True, text=True
        )
        clock = proc_res.stdout.strip()
    except Exception as e:
        clock = f"Error: {e}"

    try:
        bat_res = subprocess.run(
            ["powershell", "-Command", "Get-CimInstance -Namespace root\\wmi -ClassName BatteryStatus | Select-Object -ExpandProperty PowerOnline"],
            capture_output=True, text=True
        )
        ac = bat_res.stdout.strip()
    except Exception as e:
        ac = f"Error: {e}"

    return {"scheme": scheme, "clock_mhz": clock, "ac_online": ac}

def wait_for_ready(timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen("http://localhost/ready", timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(1)
    return False

def switch_build(build: str):
    log_telemetry(f"Switching ANALYTICS_BUILD to '{build}'...")
    env = os.environ.copy()
    env["ANALYTICS_BUILD"] = build
    cmd = ["docker", "compose", "up", "-d", "--no-deps", "api1", "api2", "api3"]
    res = subprocess.run(cmd, env=env, cwd=WORKSPACE, capture_output=True, text=True)
    if res.returncode != 0:
        log_telemetry(f"ERROR restarting api containers: {res.stderr}")
        raise RuntimeError("Failed to restart API containers")
    if not wait_for_ready():
        log_telemetry("ERROR: API failed to reach /ready state after build switch!")
        raise RuntimeError("API not ready")
    log_telemetry(f"Successfully switched to '{build}' build and API is healthy.")

def get_click_count():
    try:
        res = subprocess.run(
            ['docker', 'compose', 'exec', '-T', 'postgres', 'psql', '-U', 'urlshort', '-d', 'urlshort', '-t', '-A', '-c', 'SELECT COUNT(*) FROM clicks;'],
            cwd=WORKSPACE, capture_output=True, text=True
        )
        return int(res.stdout.strip())
    except Exception:
        return -1

def get_analytics_metrics():
    try:
        req = urllib.request.urlopen('http://localhost/metrics', timeout=2)
        text = req.read().decode('utf-8')
        dropped = 0.0
        failures = 0.0
        for line in text.split('\n'):
            if line.startswith('urlshort_analytics_dropped_clicks_total'):
                dropped = float(line.split()[1])
            elif line.startswith('urlshort_analytics_flush_failures_total'):
                failures = float(line.split()[1])
        return dropped, failures
    except Exception:
        return 0.0, 0.0

def run_k6(rate=8000, duration="120s", warmup="0s", max_vus=5000, checkpoint_at_sec=None):
    summary_file = os.path.join(WORKSPACE, "bench", "temp_summary.json")
    if os.path.exists(summary_file):
        os.remove(summary_file)

    if checkpoint_at_sec:
        def trigger_cp():
            time.sleep(checkpoint_at_sec)
            log_telemetry(f"[*] TRIGGERING FORCED CHECKPOINT at {checkpoint_at_sec}s mark...")
            t0 = time.time()
            cp_res = subprocess.run(
                ['docker', 'compose', 'exec', '-T', 'postgres', 'psql', '-U', 'urlshort', '-d', 'urlshort', '-c', 'CHECKPOINT;'],
                cwd=WORKSPACE, capture_output=True, text=True
            )
            elapsed = time.time() - t0
            log_telemetry(f"[*] FORCED CHECKPOINT completed in {elapsed:.3f}s: {cp_res.stdout.strip()}")
        t = threading.Thread(target=trigger_cp, daemon=True)
        t.start()

    bench_dir = os.path.join(WORKSPACE, "bench").replace("\\", "/")
    k6_cmd = [
        'docker', 'run', '--rm',
        '--cpus=2',
        '-w', '/bench',
        '--network', 'urlshortner_urlnet',
        '-v', f"{bench_dir}:/bench",
        'grafana/k6', 'run',
        '--env', 'BASE_URL=http://nginx',
        '--env', f'RATE={rate}',
        '--env', f'DURATION={duration}',
        '--env', f'WARMUP_DURATION={warmup}',
        '--env', f'MAX_VUS={max_vus}',
        '--summary-export=/bench/temp_summary.json',
        '/bench/bench.js'
    ]

    log_telemetry(f"Running k6: rate={rate}, duration={duration}, warmup={warmup}, max_vus={max_vus}, cpus=2")
    t0 = time.time()
    res = subprocess.run(k6_cmd, capture_output=True, text=True)
    run_duration = time.time() - t0

    # Parse summary JSON if available
    total_reqs = 0
    delivered_rate = 0.0
    p99 = "N/A"
    med = "N/A"
    dropped_iters = 0
    failures = 0

    if os.path.exists(summary_file):
        try:
            with open(summary_file, "r", encoding="utf-8") as f:
                sdata = json.load(f)
            m = sdata.get("metrics", {})
            http_m = m.get("http_reqs", {})
            total_reqs = int(http_m.get("count", 0))
            delivered_rate = float(http_m.get("rate", 0.0))
            
            lat_m = m.get("redirect_latency_ms") or m.get("http_req_duration", {})
            if "p(99)" in lat_m:
                p99 = f"{lat_m['p(99)']:.2f}ms"
            if "med" in lat_m:
                med = f"{lat_m['med']:.2f}ms"

            dropped_iters = int(m.get("dropped_iterations", {}).get("count", 0))
            failures = int(m.get("failed_redirects", {}).get("count", 0))
        except Exception as e:
            log_telemetry(f"Error reading summary JSON: {e}")

    # Fallback to regex if JSON was missing or incomplete
    if total_reqs == 0:
        stdout = res.stdout
        req_match = re.search(r'http_reqs\.+:\s+(\d+)\s+([0-9\.]+)/s', stdout)
        total_reqs = int(req_match.group(1)) if req_match else 0
        delivered_rate = float(req_match.group(2)) if req_match else 0.0

    return {
        "total_reqs": total_reqs,
        "delivered_rate": delivered_rate,
        "p99": p99,
        "med": med,
        "dropped_iters": dropped_iters,
        "failures": failures,
        "run_duration": run_duration,
        "stdout": res.stdout
    }

def parse_ms(val_str: str) -> float:
    if not val_str or val_str == "N/A":
        return 0.0
    val_str = val_str.strip()
    if val_str.endswith("ms"):
        return float(val_str[:-2])
    elif val_str.endswith("µs") or val_str.endswith("us"):
        return float(val_str[:-2]) / 1000.0
    elif val_str.endswith("s"):
        return float(val_str[:-1]) * 1000.0
    try:
        return float(val_str)
    except Exception:
        return 0.0

def run_matrix():
    import statistics
    order = ["old", "new", "old", "new", "old", "new"]
    results = []
    
    log_telemetry("=== STARTING STEP 2: FIXED-LOAD COMPARISON (OLD vs NEW) ===")
    
    for i, build in enumerate(order, start=1):
        log_telemetry(f"\n--- RUN {i}/6: Build = {build.upper()} ---")
        pre_sys = get_system_telemetry()
        log_telemetry(f"Pre-run System State: AC={pre_sys['ac_online']}, Clock={pre_sys['clock_mhz']} MHz, Scheme={pre_sys['scheme']}")
        
        switch_build(build)
        
        # Let things stabilize 5 seconds
        time.sleep(5)
        
        run_res = run_k6(rate=8000, duration="120s", warmup="0s", max_vus=5000)
        
        post_sys = get_system_telemetry()
        log_telemetry(f"Post-run System State: AC={post_sys['ac_online']}, Clock={post_sys['clock_mhz']} MHz")
        log_telemetry(f"Run {i} Result [{build}]: Delivered Rate = {run_res['delivered_rate']:.1f} req/s, p99 = {run_res['p99']}, Dropped Iters = {run_res['dropped_iters']}, Failures = {run_res['failures']}")
        
        run_data = {
            "run": i,
            "build": build,
            "delivered_rate": run_res["delivered_rate"],
            "p99": run_res["p99"],
            "p99_ms": parse_ms(run_res["p99"]),
            "med": run_res["med"],
            "med_ms": parse_ms(run_res["med"]),
            "dropped_iters": run_res["dropped_iters"],
            "failures": run_res["failures"],
            "total_reqs": run_res["total_reqs"],
            "clock_pre": pre_sys["clock_mhz"],
            "clock_post": post_sys["clock_mhz"],
        }
        results.append(run_data)
        
        # Cool down 10s between runs
        time.sleep(10)
        
    old_runs = [r for r in results if r["build"] == "old"]
    new_runs = [r for r in results if r["build"] == "new"]

    summary = {
        "runs": results,
        "medians": {
            "old": {
                "delivered_rate": statistics.median([r["delivered_rate"] for r in old_runs]),
                "p99_ms": statistics.median([r["p99_ms"] for r in old_runs]),
                "dropped_iters": statistics.median([r["dropped_iters"] for r in old_runs]),
                "failures": statistics.median([r["failures"] for r in old_runs]),
            },
            "new": {
                "delivered_rate": statistics.median([r["delivered_rate"] for r in new_runs]),
                "p99_ms": statistics.median([r["p99_ms"] for r in new_runs]),
                "dropped_iters": statistics.median([r["dropped_iters"] for r in new_runs]),
                "failures": statistics.median([r["failures"] for r in new_runs]),
            }
        }
    }

    with open("matrix_results.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
        
    log_telemetry("\n=== STEP 2 FINISHED: Summary Matrix ===")
    for r in results:
        log_telemetry(f"Run {r['run']} ({r['build'].upper()}): Rate={r['delivered_rate']:.1f}/s, p99={r['p99']}, Dropped={r['dropped_iters']}, Fails={r['failures']}")
    
    log_telemetry("\n--- MEDIANS COMPARISON ---")
    log_telemetry(f"OLD Build: Rate = {summary['medians']['old']['delivered_rate']:.1f} req/s, p99 = {summary['medians']['old']['p99_ms']:.2f} ms, Dropped = {summary['medians']['old']['dropped_iters']}, Fails = {summary['medians']['old']['failures']}")
    log_telemetry(f"NEW Build: Rate = {summary['medians']['new']['delivered_rate']:.1f} req/s, p99 = {summary['medians']['new']['p99_ms']:.2f} ms, Dropped = {summary['medians']['new']['dropped_iters']}, Fails = {summary['medians']['new']['failures']}")
    return summary

def run_checkpoint_comparison():
    log_telemetry("\n=== STARTING STEP 3: FORCED CHECKPOINT ON OLD BUILD ===")
    pre_sys = get_system_telemetry()
    log_telemetry(f"Pre-run System State: AC={pre_sys['ac_online']}, Clock={pre_sys['clock_mhz']} MHz")
    
    switch_build("old")
    time.sleep(5)
    
    c0 = get_click_count()
    d0, f0 = get_analytics_metrics()
    log_telemetry(f"Baseline clicks={c0}, dropped_metric={d0}, flush_failures_metric={f0}")
    
    # 8k load for 120s with forced checkpoint at 60s
    run_res = run_k6(rate=8000, duration="120s", warmup="0s", max_vus=5000, checkpoint_at_sec=60)
    
    time.sleep(10) # wait for settling
    
    c1 = get_click_count()
    d1, f1 = get_analytics_metrics()
    
    post_sys = get_system_telemetry()
    delta_clicks = c1 - c0
    delta_dropped = d1 - d0
    delta_failures = f1 - f0
    
    log_telemetry(f"Post-run System State: AC={post_sys['ac_online']}, Clock={post_sys['clock_mhz']} MHz")
    log_telemetry(f"Old Checkpoint Results:")
    log_telemetry(f"  Delivered Rate: {run_res['delivered_rate']:.1f} req/s")
    log_telemetry(f"  Total Requests: {run_res['total_reqs']}")
    log_telemetry(f"  p99 Latency: {run_res['p99']}")
    log_telemetry(f"  Dropped Iterations: {run_res['dropped_iters']}")
    log_telemetry(f"  k6 Failures: {run_res['failures']}")
    log_telemetry(f"  Clicks Inserted: {delta_clicks}")
    log_telemetry(f"  Flush Failures Metric Delta: {delta_failures}")
    log_telemetry(f"  Dropped Clicks Metric Delta: {delta_dropped}")
    
    cp_data = {
        "build": "old",
        "delivered_rate": run_res["delivered_rate"],
        "total_reqs": run_res["total_reqs"],
        "p99": run_res["p99"],
        "dropped_iters": run_res["dropped_iters"],
        "failures": run_res["failures"],
        "clicks_inserted": delta_clicks,
        "flush_failures": delta_failures,
        "dropped_clicks": delta_dropped,
    }
    with open("checkpoint_old_results.json", "w", encoding="utf-8") as f:
        json.dump(cp_data, f, indent=2)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "checkpoint":
        run_checkpoint_comparison()
    else:
        run_matrix()
