"""Finish check 4 (batch ceiling) at safe pacing, and quantify how many golf
booking apps carry a vendor signal in the CHEAP iTunes JSON alone (bears on
whether Phase 3's per-app HTML fetch is required for every candidate)."""
import json
import os
import re
import time
import urllib.parse

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "..", "docs", "raw")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36 golfapps-research/0.1 (+derek@downgrain.com)")
S = requests.Session()
S.headers["User-Agent"] = UA
DELAY = 6.0  # 10 rpm — deliberately below anything measured as contended
_last = [0.0]


def get(url, tag=None, tries=7):
    for a in range(tries):
        w = DELAY - (time.time() - _last[0])
        if w > 0:
            time.sleep(w)
        r = S.get(url, timeout=60)
        _last[0] = time.time()
        if r.status_code == 200:
            if tag:
                with open(os.path.join(RAW, f"{tag}.json"), "w", encoding="utf-8") as f:
                    f.write(r.text)
            return r
        back = min(300, 30 * (2 ** a))
        print(f"    {r.status_code} on try {a}; backing off {back}s")
        time.sleep(back)
    return r


def jget(url, tag=None):
    r = get(url, tag)
    try:
        return r.json()
    except Exception:
        return json.loads(r.text.strip())


report = {}

print("=== Build a pool of real golf-app ids (checkpointed) ===")
POOL_F = os.path.join(RAW, "pool_rows.json")
rows = {}
if os.path.exists(POOL_F):
    rows = {int(k): v for k, v in json.load(open(POOL_F, encoding="utf-8")).items()}
    print(f"  resumed {len(rows)} rows from disk")
pool = list(rows.keys())
for term in ["golf", "tee times", "golf club", "country club", "golf course",
             "golf booking", "book tee times", "golf tee times"]:
    d = jget("https://itunes.apple.com/search?term=" + urllib.parse.quote_plus(term)
             + "&entity=software&country=us&limit=200")
    for x in d.get("results", []):
        if x.get("trackId") and x["trackId"] not in rows:
            rows[x["trackId"]] = x
            pool.append(x["trackId"])
    print(f"  +{term!r:20s} pool={len(pool)}")
    with open(POOL_F, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=1, ensure_ascii=False)
report["pool_size"] = len(pool)

print("\n=== CHECK 4 (finish): batch lookup ceiling ===")
batch = {}
for n in (200, 300, 400, 500, 600):
    if len(pool) < n:
        batch[f"batch_{n}"] = {"skipped": f"pool={len(pool)}"}
        continue
    ids = pool[:n]
    url = "https://itunes.apple.com/lookup?id=" + ",".join(map(str, ids)) + "&country=us"
    t = time.time()
    r = get(url)
    el = time.time() - t
    try:
        d = r.json()
    except Exception:
        try:
            d = json.loads(r.text.strip())
        except Exception:
            batch[f"batch_{n}"] = {"http": r.status_code, "url_len": len(url),
                                   "unparseable": True, "body_head": r.text[:160]}
            print(f"  {n}: {batch[f'batch_{n}']}")
            continue
    got = {x.get("trackId") for x in d.get("results", [])
           if x.get("wrapperType") == "software"}
    batch[f"batch_{n}"] = {
        "http": r.status_code, "url_len": len(url), "requested": n,
        "returned": len(got), "secs": round(el, 2),
        "TRUNCATED": len(got) < n, "n_missing": n - len(got),
    }
    print(f"  {n}: {batch[f'batch_{n}']}")
report["check4_batch_ceiling"] = batch

print("\n=== Vendor signal coverage in the CHEAP JSON ===")
VENDORS = {
    "sagacity":   {"tok": ["sagacity", "quick18", "quick 18"],
                   "bundle": ["com.quick18."]},
    "gallus":     {"tok": ["gallus"], "bundle": ["com.gallusgolf."]},
    "chronogolf": {"tok": ["chronogolf"], "bundle": ["com.chronogolf."]},
    "foreup":     {"tok": ["foreup"], "bundle": ["com.foreup"]},
    "teesnap":    {"tok": ["teesnap"], "bundle": ["com.teesnap"]},
    "clubprophet": {"tok": ["club prophet", "clubprophet"], "bundle": ["com.clubprophet"]},
}


def signals(x):
    fields = {
        "sellerUrl": (x.get("sellerUrl") or "").lower(),
        "bundleId": (x.get("bundleId") or "").lower(),
        "artistName": (x.get("artistName") or "").lower(),
        "trackName": (x.get("trackName") or "").lower(),
        "description": (x.get("description") or "").lower(),
    }
    hits = {}
    for v, cfg in VENDORS.items():
        for fname, val in fields.items():
            if any(t in val for t in cfg["tok"]) or \
               (fname == "bundleId" and any(val.startswith(b) for b in cfg["bundle"])):
                hits.setdefault(v, []).append(fname)
    return {k: sorted(set(vv)) for k, vv in hits.items()}


# Booking-app heuristic: description states it books tee times for a course.
BOOKISH = re.compile(r"(?i)(tee time|book(ing)? .{0,20}tee|reservation)")
tally = {"total_pool": len(pool), "bookish": 0, "with_vendor_signal": 0,
         "by_vendor": {}, "by_field": {}, "signal_only_from_sellerUrl": 0,
         "signal_only_from_bundleId": 0, "bookish_no_signal": []}
for tid in pool:
    x = rows[tid]
    desc = (x.get("description") or "") + " " + (x.get("trackName") or "")
    if not BOOKISH.search(desc):
        continue
    tally["bookish"] += 1
    h = signals(x)
    if h:
        tally["with_vendor_signal"] += 1
        for v, fs in h.items():
            tally["by_vendor"][v] = tally["by_vendor"].get(v, 0) + 1
            for f in fs:
                tally["by_field"][f] = tally["by_field"].get(f, 0) + 1
        allf = {f for fs in h.values() for f in fs}
        if allf == {"sellerUrl"}:
            tally["signal_only_from_sellerUrl"] += 1
        if allf == {"bundleId"}:
            tally["signal_only_from_bundleId"] += 1
    else:
        if len(tally["bookish_no_signal"]) < 25:
            tally["bookish_no_signal"].append({
                "trackId": tid, "trackName": x.get("trackName"),
                "artistName": x.get("artistName"), "bundleId": x.get("bundleId"),
                "sellerUrl": x.get("sellerUrl")})
report["cheap_json_signal_coverage"] = tally
print(json.dumps({k: v for k, v in tally.items() if k != "bookish_no_signal"},
                 indent=2))

with open(POOL_F, "w", encoding="utf-8") as f:
    json.dump(rows, f, indent=1, ensure_ascii=False)
p = os.path.join(RAW, "batch_and_signal_summary.json")
with open(p, "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2, ensure_ascii=False)
print("\nwrote", p)
