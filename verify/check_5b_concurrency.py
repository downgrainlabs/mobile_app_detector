"""Check 5b: is ~20 req/min a per-IP rate LIMIT or just single-connection latency?

Runs the same workload at 1 / 4 / 8 / 16 concurrent workers from ONE IP and
compares achieved throughput and error rates. Also nails down the batch-lookup
ceiling above 200 ids.
"""
import json
import os
import statistics
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "..", "docs", "raw")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36 golfapps-research/0.1 (+derek@downgrain.com)")

WORDS = ["golf", "tee times", "golf club", "country club", "golf booking", "public golf",
         "municipal golf", "golf resort", "links", "golf gps", "fairway", "clubhouse",
         "pro shop", "scorecard", "driving range", "golf academy"]
STATES = ["texas", "florida", "california", "arizona", "colorado", "ohio", "michigan",
          "georgia", "carolina", "nevada", "utah", "oregon", "idaho", "maine", "iowa",
          "kansas"]


def one(sess, i):
    term = f"{WORDS[i % len(WORDS)]} {STATES[(i // len(WORDS)) % len(STATES)]}"
    url = ("https://itunes.apple.com/search?term=" + urllib.parse.quote_plus(term)
           + "&entity=software&country=us&limit=200")
    t = time.time()
    try:
        r = sess.get(url, timeout=40)
        return {"status": r.status_code, "secs": time.time() - t, "bytes": len(r.content)}
    except Exception as e:
        return {"status": f"EXC:{type(e).__name__}", "secs": time.time() - t, "bytes": 0}


def run_at(workers, n):
    sessions = [requests.Session() for _ in range(workers)]
    for s in sessions:
        s.headers["User-Agent"] = UA
        # allow the pool to actually hold `workers` sockets
        s.mount("https://", requests.adapters.HTTPAdapter(pool_connections=workers,
                                                          pool_maxsize=workers))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        out = list(ex.map(lambda i: one(sessions[i % workers], i), range(n)))
    el = time.time() - t0
    codes = {}
    for o in out:
        codes[str(o["status"])] = codes.get(str(o["status"]), 0) + 1
    lat = [o["secs"] for o in out if o["status"] == 200]
    res = {
        "workers": workers, "requests": n, "elapsed_s": round(el, 2),
        "throughput_req_per_min": round(n / el * 60, 1),
        "status_counts": codes,
        "ok": codes.get("200", 0),
        "median_latency_s": round(statistics.median(lat), 2) if lat else None,
        "p95_latency_s": round(sorted(lat)[int(len(lat) * .95) - 1], 2) if len(lat) > 3 else None,
    }
    print(f"  workers={workers:2d}  {n} reqs in {el:6.1f}s = "
          f"{res['throughput_req_per_min']:6.1f} req/min | median {res['median_latency_s']}s "
          f"| {codes}")
    return res


report = {}
print("=== Concurrency ramp on itunes.apple.com/search (single IP) ===")
ramp = []
for w in (1, 4, 8, 16):
    ramp.append(run_at(w, 32))
    time.sleep(10)  # let anything transient settle between tiers
report["concurrency_ramp_itunes_search"] = ramp

print("\n=== Sustained run at 8 workers (128 requests) — does a limit kick in late? ===")
report["sustained_8w"] = run_at(8, 128)

print("\n=== Batch lookup ceiling above 200 ids ===")
s = requests.Session()
s.headers["User-Agent"] = UA
pool = []
for term in ["golf", "tee times", "golf club", "country club", "golf course"]:
    r = s.get("https://itunes.apple.com/search?term=" + urllib.parse.quote_plus(term)
              + "&entity=software&country=us&limit=200", timeout=40)
    try:
        d = r.json()
    except Exception:
        d = json.loads(r.text.strip())
    for x in d.get("results", []):
        if x.get("trackId") and x["trackId"] not in pool:
            pool.append(x["trackId"])
    time.sleep(3.5)
print(f"  pool size = {len(pool)}")

batch = {"pool_size": len(pool)}
for n in (200, 300, 400, 500, 600):
    if len(pool) < n:
        batch[f"batch_{n}"] = {"skipped": f"pool={len(pool)}"}
        continue
    ids = pool[:n]
    url = "https://itunes.apple.com/lookup?id=" + ",".join(map(str, ids)) + "&country=us"
    t = time.time()
    r = s.get(url, timeout=60)
    el = time.time() - t
    try:
        d = r.json()
        got = [x.get("trackId") for x in d.get("results", [])
               if x.get("wrapperType") == "software"]
        batch[f"batch_{n}"] = {"http": r.status_code, "url_len": len(url),
                               "requested": n, "returned": len(got),
                               "secs": round(el, 2),
                               "TRUNCATED": len(got) < n,
                               "n_missing": n - len(got)}
    except Exception as e:
        batch[f"batch_{n}"] = {"http": r.status_code, "url_len": len(url),
                               "error": str(e), "body_head": r.text[:200],
                               "secs": round(el, 2)}
    print(f"  batch {n}: {batch[f'batch_{n}']}")
    time.sleep(3.5)
report["batch_ceiling"] = batch

p = os.path.join(RAW, "concurrency_summary.json")
with open(p, "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2)
print("\nwrote", p)
