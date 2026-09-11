"""Check 5: empirically locate the rate limit on itunes.apple.com and apps.apple.com.

Bounded probe: sequential unthrottled bursts, stops on sustained blocking,
then measures recovery. Total request budget is capped.
"""
import json
import os
import time
import urllib.parse

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "..", "docs", "raw")
os.makedirs(RAW, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36 golfapps-research/0.1 (+derek@downgrain.com)")

TERMS = ["golf course", "tee times", "golf club", "country club", "golf booking",
         "public golf", "municipal golf", "golf resort", "links golf", "golf gps"]

report = {}


def burst(name, url_fn, n_max, stop_after_blocked=5):
    """Fire sequential requests with no delay; log every status."""
    s = requests.Session()
    s.headers["User-Agent"] = UA
    log = []
    blocked_streak = 0
    t0 = time.time()
    first_block = None
    for i in range(n_max):
        url = url_fn(i)
        t = time.time()
        try:
            r = s.get(url, timeout=30)
            code, size = r.status_code, len(r.content)
            ra = r.headers.get("Retry-After")
        except Exception as e:
            code, size, ra = f"EXC:{type(e).__name__}", 0, None
        el = time.time() - t
        log.append({"i": i, "t_since_start": round(time.time() - t0, 2),
                    "status": code, "bytes": size, "secs": round(el, 2),
                    "retry_after": ra})
        ok = code == 200
        if not ok:
            blocked_streak += 1
            if first_block is None:
                first_block = {"i": i, "t": round(time.time() - t0, 2), "status": code}
                print(f"  !! first non-200 at request #{i} "
                      f"({round(time.time()-t0,2)}s in) status={code}")
            if blocked_streak >= stop_after_blocked:
                print(f"  !! {blocked_streak} consecutive blocks — stopping burst")
                break
        else:
            blocked_streak = 0
        if i % 20 == 0:
            rate = (i + 1) / max(time.time() - t0, 0.001) * 60
            print(f"  #{i:3d} {code}  elapsed={time.time()-t0:6.1f}s  "
                  f"rate={rate:5.0f} req/min")
    total = time.time() - t0
    n = len(log)
    codes = {}
    for e in log:
        codes[str(e["status"])] = codes.get(str(e["status"]), 0) + 1
    res = {
        "requests_sent": n,
        "elapsed_s": round(total, 2),
        "achieved_rate_req_per_min": round(n / max(total, 0.001) * 60, 1),
        "status_counts": codes,
        "first_non_200": first_block,
        "n_200": codes.get("200", 0),
        "log": log,
    }
    print(f"  == {name}: {n} reqs in {total:.1f}s = "
          f"{res['achieved_rate_req_per_min']:.0f} req/min, "
          f"{codes.get('200',0)} OK, first block: {first_block}")
    return res


print("=== BURST 1: itunes.apple.com/search, unthrottled, max 120 ===")
report["burst_itunes_search"] = burst(
    "itunes /search",
    lambda i: ("https://itunes.apple.com/search?term="
               + urllib.parse.quote_plus(TERMS[i % len(TERMS)] + f" {i}")
               + "&entity=software&country=us&limit=200"),
    120,
)

print("\n=== RECOVERY probe after burst 1 ===")
rec = []
s = requests.Session()
s.headers["User-Agent"] = UA
t0 = time.time()
for i in range(12):
    try:
        r = s.get("https://itunes.apple.com/lookup?id=991127971&country=us", timeout=30)
        code = r.status_code
    except Exception as e:
        code = f"EXC:{type(e).__name__}"
    rec.append({"t_after_burst_s": round(time.time() - t0, 1), "status": code})
    print(f"  +{time.time()-t0:5.1f}s -> {code}")
    if code == 200 and i >= 1 and rec[-2]["status"] == 200:
        break
    time.sleep(15)
report["recovery_after_itunes_burst"] = rec

print("\n=== BURST 2: apps.apple.com product HTML, unthrottled, max 60 ===")
# Real ids from the Quick 18 account so the pages exist.
IDS = [551470750, 541610141, 573344558, 514422555, 611268203, 594297249,
       1072881519, 991127971, 609888522, 6479811159, 1660735735, 1358773907]
report["burst_apps_html"] = burst(
    "apps.apple.com HTML",
    lambda i: f"https://apps.apple.com/us/app/id{IDS[i % len(IDS)]}",
    60,
)

print("\n=== RECOVERY probe after burst 2 ===")
rec2 = []
t0 = time.time()
for i in range(8):
    try:
        r = requests.get("https://apps.apple.com/us/app/id991127971",
                         headers={"User-Agent": UA}, timeout=30)
        code = r.status_code
    except Exception as e:
        code = f"EXC:{type(e).__name__}"
    rec2.append({"t_after_burst_s": round(time.time() - t0, 1), "status": code})
    print(f"  +{time.time()-t0:5.1f}s -> {code}")
    if code == 200:
        break
    time.sleep(15)
report["recovery_after_html_burst"] = rec2

p = os.path.join(RAW, "ratelimit_summary.json")
with open(p, "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2)
print("\nwrote", p)
