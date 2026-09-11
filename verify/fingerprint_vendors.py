"""Derive App Store fingerprints for the priority vendor list.

Two passes:
  1. OFFLINE — scan the 959-app corpus already on disk for each vendor's tokens.
  2. LIVE    — targeted /search per vendor name (private-club apps often say
               nothing about golf, so the generic sweep misses them).

Output: per vendor, the bundle prefixes / seller domains / artist accounts that
identify it — i.e. the raw material for vendors.yaml.
"""
import json
import os
import re
import time
import urllib.parse
from collections import defaultdict

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "..", "docs", "raw")
S = requests.Session()
S.headers["User-Agent"] = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 "
                           "golfapps-research/0.1 (+derek@downgrain.com)")
DELAY = 2.5           # ~24 rpm — measured clean at 30
_last = [0.0]


def jget(url, tries=5):
    for a in range(tries):
        w = DELAY - (time.time() - _last[0])
        if w > 0:
            time.sleep(w)
        r = S.get(url, timeout=45)
        _last[0] = time.time()
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return json.loads(r.text.strip())
        back = min(300, 30 * (2 ** a))
        print(f"      {r.status_code}; backoff {back}s")
        time.sleep(back)
    return {"results": []}


# Priority list from Derek, 2026-08-18. `tok` are substrings matched against
# bundleId / sellerUrl / artistName / sellerName / trackName / description.
# `queries` are /search terms — deliberately include non-golf phrasings.
VENDORS = {
    "northstar":     {"tok": ["northstar", "north star"],
                      "queries": ["northstar club", "northstar golf",
                                  "northstar technologies club"]},
    "cobalt":        {"tok": ["cobalt"],
                      "queries": ["cobalt club", "cobalt software club",
                                  "cobalt private club"]},
    "foretees":      {"tok": ["foretees", "fore tees"],
                      "queries": ["foretees", "foretees mobile"]},
    "buzclub":       {"tok": ["buz club", "buzclub", "thebuz"],
                      "queries": ["buz club", "the buz club", "buz club software"]},
    "whoosh":        {"tok": ["whoosh"],
                      "queries": ["whoosh golf", "whoosh club", "whoosh caddie"]},
    "sagacity":      {"tok": ["sagacity", "quick18", "quick 18"],
                      "queries": ["sagacity golf", "quick 18 golf"]},
    "gallus":        {"tok": ["gallus"], "queries": ["gallus golf"]},
    "clubessential": {"tok": ["clubessential", "club essential"],
                      "queries": ["clubessential", "club essential"]},
    "jonas":         {"tok": ["jonassoftware", "jonas club", "jonasclub"],
                      "queries": ["jonas club software", "jonas club"]},
    "clubhouseonline": {"tok": ["clubhouseonline", "clubhouse online"],
                        "queries": ["clubhouse online", "clubhouse online e3"]},
    "membersfirst":  {"tok": ["membersfirst", "members first"],
                      "queries": ["membersfirst", "members first club"]},
    # Also in scope, per follow-up: the four the original plan named.
    "chronogolf":    {"tok": ["chronogolf", "chrono golf"],
                      "queries": ["chronogolf", "chronogolf lightspeed"]},
    "foreup":        {"tok": ["foreup", "fore up"],
                      "queries": ["foreup", "foreup golf", "foreup tee times"]},
    "teesnap":       {"tok": ["teesnap", "tee snap"],
                      "queries": ["teesnap", "teesnap golf"]},
    "clubprophet":   {"tok": ["clubprophet", "club prophet"],
                      "queries": ["club prophet", "club prophet systems"]},
}
FIELDS = ["bundleId", "sellerUrl", "artistName", "sellerName", "trackName", "description"]


def matches(x, toks):
    hit = []
    for f in FIELDS:
        v = (str(x.get(f) or "")).lower()
        if any(t in v for t in toks):
            hit.append(f)
    return hit


def prefix(b):
    p = (b or "").lower().split(".")
    return ".".join(p[:2]) if len(p) >= 2 else (b or "").lower()


def domain(u):
    m = re.match(r"https?://([^/]+)", (u or "").strip(), re.I)
    if not m:
        return None
    h = m.group(1).lower()
    h = h[4:] if h.startswith("www.") else h
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


# ---------------------------------------------------------------- pass 1
pool = json.load(open(os.path.join(RAW, "pool_rows.json"), encoding="utf-8"))
print(f"OFFLINE pass over {len(pool)} corpus apps\n")
found = {v: {} for v in VENDORS}
for tid, x in pool.items():
    for v, cfg in VENDORS.items():
        if matches(x, cfg["tok"]):
            found[v][str(tid)] = x

# ---------------------------------------------------------------- pass 2
print("LIVE pass — targeted searches\n")
for v, cfg in VENDORS.items():
    for q in cfg["queries"]:
        d = jget("https://itunes.apple.com/search?term=" + urllib.parse.quote_plus(q)
                 + "&entity=software&country=us&limit=200")
        new = 0
        for x in d.get("results", []):
            if matches(x, cfg["tok"]) and str(x.get("trackId")) not in found[v]:
                found[v][str(x["trackId"])] = x
                new += 1
        print(f"  {v:16s} {q!r:34s} results={len(d.get('results',[])):4d} new={new}")

# artistId expansion for any account that looks vendor-owned
print("\nartistId expansion\n")
for v, cfg in VENDORS.items():
    counts = defaultdict(int)
    for x in found[v].values():
        if x.get("artistId"):
            counts[x["artistId"]] += 1
    for aid, n in sorted(counts.items(), key=lambda kv: -kv[1])[:2]:
        if n < 3:
            continue
        d = jget(f"https://itunes.apple.com/lookup?id={aid}&entity=software"
                 f"&limit=200&country=us")
        new = 0
        for x in d.get("results", []):
            if x.get("wrapperType") == "software" and str(x.get("trackId")) not in found[v]:
                found[v][str(x["trackId"])] = x
                new += 1
        print(f"  {v:16s} artistId={aid} held {n} -> +{new} more")

# ---------------------------------------------------------------- report
report = {}
print("\n" + "=" * 78)
for v, apps in found.items():
    pref, dom, artists = defaultdict(int), defaultdict(int), defaultdict(int)
    for x in apps.values():
        if x.get("bundleId"):
            pref[prefix(x["bundleId"])] += 1
        if domain(x.get("sellerUrl")):
            dom[domain(x["sellerUrl"])] += 1
        if x.get("artistName"):
            artists[x["artistName"]] += 1
    rec = {
        "n_apps": len(apps),
        "bundle_prefixes": dict(sorted(pref.items(), key=lambda kv: -kv[1])[:6]),
        "seller_domains": dict(sorted(dom.items(), key=lambda kv: -kv[1])[:6]),
        "n_distinct_operators": len(artists),
        "top_operators": dict(sorted(artists.items(), key=lambda kv: -kv[1])[:4]),
        "publishing_model": ("vendor_account" if artists and
                             max(artists.values()) / max(len(apps), 1) > .5
                             else "operator_account"),
        "sample": [{"trackId": k, "name": x.get("trackName"),
                    "bundleId": x.get("bundleId"), "sellerUrl": x.get("sellerUrl"),
                    "artistName": x.get("artistName")}
                   for k, x in list(apps.items())[:4]],
    }
    report[v] = rec
    status = "FOUND" if apps else "*** NOT FOUND ***"
    print(f"\n{v.upper():18s} {status}  apps={len(apps)}  "
          f"operators={len(artists)}  model={rec['publishing_model']}")
    if pref:
        print(f"  bundle prefixes : {dict(list(rec['bundle_prefixes'].items())[:4])}")
    if dom:
        print(f"  seller domains  : {dict(list(rec['seller_domains'].items())[:4])}")
    for s in rec["sample"][:3]:
        print(f"    - {str(s['name'])[:38]:40s} {str(s['bundleId'])[:38]}")

with open(os.path.join(RAW, "vendor_fingerprints.json"), "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2, ensure_ascii=False)
print("\nwrote docs/raw/vendor_fingerprints.json")
