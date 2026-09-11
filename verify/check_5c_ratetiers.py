"""Check 5c: characterise the 429 discovered at 8 sustained workers.

Measures: cooldown time, Retry-After semantics, and the 429 rate at several
fixed target request rates so the pipeline can be paced from evidence.
"""
import json
import os
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "..", "docs", "raw")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36 golfapps-research/0.1 (+derek@downgrain.com)")

WORDS = ["golf", "tee times", "golf club", "country club", "golf booking", "public golf",
         "municipal golf", "golf resort", "links", "golf gps", "fairway", "clubhouse"]
STATES = ["texas", "florida", "california", "arizona", "colorado", "ohio", "michigan",
          "georgia", "carolina", "nevada", "utah", "oregon"]
_n = [0]


def url_for():
    i = _n[0]
    _n[0] += 1
    term = f"{WORDS[i % len(WORDS)]} {STATES[(i // len(WORDS)) % len(STATES)]} {i}"
    return ("https://itunes.apple.com/search?term=" + urllib.parse.quote_plus(term)
            + "&entity=software&country=us&limit=200")


report = {}

# ------------------------------------------------- capture a 429 in full
print("=== Cooldown: how long until 429s stop? ===")
s = requests.Session()
s.headers["User-Agent"] = UA
cool = []
t0 = time.time()
sample_429 = None
for i in range(40):
    r = s.get("https://itunes.apple.com/lookup?id=991127971&country=us", timeout=30)
    cool.append({"t_s": round(time.time() - t0, 1), "status": r.status_code})
    if r.status_code == 429 and sample_429 is None:
        sample_429 = {"status": 429, "headers": dict(r.headers), "body_head": r.text[:400]}
        print("  captured 429 headers:", json.dumps(dict(r.headers), indent=2)[:600])
    print(f"  +{time.time()-t0:6.1f}s -> {r.status_code}")
    if r.status_code == 200:
        break
    time.sleep(10)
report["cooldown_after_8w_burst"] = {"log": cool,
                                     "recovered_after_s": cool[-1]["t_s"],
                                     "sample_429": sample_429}

print("\n  settling 30s before rate tiers...")
time.sleep(30)


def timed_run(target_rpm, n, workers=4):
    """Send n requests paced at target_rpm using `workers` threads."""
    interval = 60.0 / target_rpm
    sess = [requests.Session() for _ in range(workers)]
    for x in sess:
        x.headers["User-Agent"] = UA
        x.mount("https://", requests.adapters.HTTPAdapter(pool_connections=workers,
                                                          pool_maxsize=workers))
    t0 = time.time()
    results = []

    def task(i):
        due = t0 + i * interval
        d = due - time.time()
        if d > 0:
            time.sleep(d)
        try:
            r = sess[i % workers].get(url_for(), timeout=40)
            return {"status": r.status_code,
                    "retry_after": r.headers.get("Retry-After"),
                    "t": round(time.time() - t0, 2)}
        except Exception as e:
            return {"status": f"EXC:{type(e).__name__}", "t": round(time.time() - t0, 2)}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(task, range(n)))
    el = time.time() - t0
    codes = {}
    for r in results:
        codes[str(r["status"])] = codes.get(str(r["status"]), 0) + 1
    n429 = codes.get("429", 0)
    out = {"target_rpm": target_rpm, "workers": workers, "requests": n,
           "elapsed_s": round(el, 1),
           "actual_rpm": round(n / el * 60, 1),
           "status_counts": codes,
           "pct_429": round(n429 / n * 100, 1),
           "first_429_at_s": next((r["t"] for r in results if r["status"] == 429), None),
           "retry_after_values": sorted({r.get("retry_after") for r in results
                                         if r.get("retry_after")}),
           }
    print(f"  target={target_rpm:4d} rpm -> actual {out['actual_rpm']:6.1f} rpm | "
          f"{out['pct_429']:5.1f}% 429 | {codes}")
    return out


print("\n=== Sustained rate tiers (60 requests each) ===")
tiers = []
for rpm in (30, 60, 120, 240):
    tiers.append(timed_run(rpm, 60))
    # cool down to a clean slate between tiers
    print("    cooling down...")
    for _ in range(30):
        time.sleep(10)
        rr = s.get("https://itunes.apple.com/lookup?id=991127971&country=us", timeout=30)
        if rr.status_code == 200:
            break
    time.sleep(20)
report["rate_tiers"] = tiers

# ------------------------------------------------- batch ceiling, retried
print("\n=== Batch lookup ceiling (retry-aware) ===")


def safe_get(url, tries=6):
    for a in range(tries):
        r = s.get(url, timeout=60)
        if r.status_code == 200:
            return r
        print(f"    {r.status_code}, backoff {2**a * 5}s")
        time.sleep(2 ** a * 5)
    return r


pool = []
for term in ["golf", "tee times", "golf club", "country club", "golf course",
             "golf app", "golf booking"]:
    r = safe_get("https://itunes.apple.com/search?term=" + urllib.parse.quote_plus(term)
                 + "&entity=software&country=us&limit=200")
    try:
        d = r.json()
    except Exception:
        d = json.loads(r.text.strip())
    for x in d.get("results", []):
        if x.get("trackId") and x["trackId"] not in pool:
            pool.append(x["trackId"])
    time.sleep(4)
print(f"  pool = {len(pool)} ids")

batch = {"pool_size": len(pool)}
for n in (200, 300, 400, 500, 600, 700):
    if len(pool) < n:
        batch[f"batch_{n}"] = {"skipped": f"pool={len(pool)}"}
        continue
    ids = pool[:n]
    url = "https://itunes.apple.com/lookup?id=" + ",".join(map(str, ids)) + "&country=us"
    t = time.time()
    r = safe_get(url)
    el = time.time() - t
    try:
        d = r.json()
        got = [x.get("trackId") for x in d.get("results", [])
               if x.get("wrapperType") == "software"]
        batch[f"batch_{n}"] = {"http": r.status_code, "url_len": len(url), "requested": n,
                               "returned": len(got), "secs": round(el, 2),
                               "TRUNCATED": len(got) < n, "n_missing": n - len(got)}
    except Exception as e:
        batch[f"batch_{n}"] = {"http": r.status_code, "url_len": len(url),
                               "parse_error": str(e)[:120], "body_head": r.text[:200]}
    print(f"  {n}: {batch[f'batch_{n}']}")
    time.sleep(4)
report["batch_ceiling"] = batch

p = os.path.join(RAW, "ratetiers_summary.json")
with open(p, "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2)
print("\nwrote", p)
