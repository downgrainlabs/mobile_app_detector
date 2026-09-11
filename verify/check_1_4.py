"""Verification checks 1-4 from golf-app-vendor-detection-plan.md.

Runs against the live iTunes API. Writes raw responses to docs/raw/ as evidence.
Conservative pacing (3.5s between calls) so this cannot itself trip a rate limit.
"""
import json
import os
import sys
import time
import urllib.parse

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "..", "docs", "raw")
os.makedirs(RAW, exist_ok=True)

UA = "golfapps-research/0.1 (+derek@downgrain.com) verification probe"
S = requests.Session()
S.headers["User-Agent"] = UA

DELAY = 3.5
_last = [0.0]


def get(url, tag):
    wait = DELAY - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    t0 = time.time()
    r = S.get(url, timeout=30)
    _last[0] = time.time()
    elapsed = _last[0] - t0
    path = os.path.join(RAW, tag + ".json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(r.text)
    print(f"[{tag}] {r.status_code} {len(r.content)}B {elapsed:.2f}s  {url[:110]}")
    return r


def jload(r):
    try:
        return r.json()
    except Exception:
        # iTunes sometimes returns text/javascript with trailing whitespace
        return json.loads(r.text.strip())


results = {}

# ---------------------------------------------------------------- CHECK 1
print("\n=== CHECK 1: iTunes lookup JSON field inventory ===")
c1 = {}
for app_id, name in [
    ("1660735735", "sagacity360"),
    ("991127971", "thorncreek"),
    ("1358773907", "los_serranos"),
]:
    r = get(
        f"https://itunes.apple.com/lookup?id={app_id}&country=us",
        f"c1_lookup_{name}_{app_id}",
    )
    d = jload(r)
    if d.get("resultCount", 0) < 1:
        c1[name] = {"error": "no results", "raw": d}
        continue
    res = d["results"][0]
    c1[name] = {
        "trackId": res.get("trackId"),
        "trackName": res.get("trackName"),
        "all_keys": sorted(res.keys()),
        "has_sellerUrl": "sellerUrl" in res,
        "sellerUrl": res.get("sellerUrl"),
        "has_privacyPolicyUrl": "privacyPolicyUrl" in res,
        "privacyPolicyUrl": res.get("privacyPolicyUrl"),
        "has_description": "description" in res,
        "description_len": len(res.get("description") or ""),
        "has_copyright": "copyright" in res,
        "copyright": res.get("copyright"),
        "sellerName": res.get("sellerName"),
        "artistId": res.get("artistId"),
        "artistName": res.get("artistName"),
        "bundleId": res.get("bundleId"),
        "artistViewUrl": res.get("artistViewUrl"),
        "supportedDevices_n": len(res.get("supportedDevices") or []),
    }
results["check1"] = c1

# ---------------------------------------------------------------- CHECK 2
print("\n=== CHECK 2: does /search index descriptions? ===")
c2 = {}
KNOWN = {991127971: "Thorncreek", 1358773907: "Los Serranos", 1660735735: "Sagacity 360"}
phrases = [
    "share these reservations with your playing partners",
    "share these reservations with your playing partners via text and email",
    "reservations with your playing partners",
]
for i, phrase in enumerate(phrases):
    q = urllib.parse.quote_plus(phrase)
    r = get(
        f"https://itunes.apple.com/search?term={q}&entity=software&country=us&limit=200",
        f"c2_search_phrase{i}",
    )
    d = jload(r)
    ids = [x.get("trackId") for x in d.get("results", [])]
    c2[phrase] = {
        "http": r.status_code,
        "resultCount": d.get("resultCount"),
        "n_results": len(d.get("results", [])),
        "known_apps_found": {str(k): (k in ids) for k in KNOWN},
        "first_10": [
            {"trackId": x.get("trackId"), "trackName": x.get("trackName"),
             "sellerName": x.get("sellerName")}
            for x in d.get("results", [])[:10]
        ],
    }

# control: does the exact track name come back? (proves search works at all)
r = get(
    "https://itunes.apple.com/search?term=" + urllib.parse.quote_plus("Thorncreek Golf Tee Times")
    + "&entity=software&country=us&limit=200",
    "c2_search_control_trackname",
)
d = jload(r)
ids = [x.get("trackId") for x in d.get("results", [])]
c2["CONTROL: trackname 'Thorncreek Golf Tee Times'"] = {
    "resultCount": d.get("resultCount"),
    "thorncreek_991127971_found": 991127971 in ids,
    "first_5": [{"trackId": x.get("trackId"), "trackName": x.get("trackName")}
                for x in d.get("results", [])[:5]],
}
results["check2"] = c2

# ---------------------------------------------------------------- CHECK 3
print("\n=== CHECK 3: limit/offset pagination on /search ===")
c3 = {}
BASE = "https://itunes.apple.com/search?term=golf&entity=software&country=us"
r = get(BASE + "&limit=200", "c3_limit200_offset0")
d0 = jload(r)
ids0 = [x.get("trackId") for x in d0.get("results", [])]
c3["limit=200 offset=(none)"] = {
    "resultCount": d0.get("resultCount"), "n_results": len(ids0),
    "first_id": ids0[0] if ids0 else None, "last_id": ids0[-1] if ids0 else None,
}

r = get(BASE + "&limit=200&offset=200", "c3_limit200_offset200")
d1 = jload(r)
ids1 = [x.get("trackId") for x in d1.get("results", [])]
overlap = len(set(ids0) & set(ids1))
c3["limit=200 offset=200"] = {
    "resultCount": d1.get("resultCount"), "n_results": len(ids1),
    "first_id": ids1[0] if ids1 else None, "last_id": ids1[-1] if ids1 else None,
    "overlap_with_offset0": overlap,
    "identical_to_offset0": ids0 == ids1,
    "PAGINATES": len(ids1) > 0 and overlap == 0,
}

r = get(BASE + "&limit=200&offset=50", "c3_limit200_offset50")
d2 = jload(r)
ids2 = [x.get("trackId") for x in d2.get("results", [])]
c3["limit=200 offset=50"] = {
    "n_results": len(ids2),
    "first_id": ids2[0] if ids2 else None,
    "equals_ids0_shifted_by_50": ids2[:20] == ids0[50:70] if len(ids0) >= 70 else None,
}

r = get(BASE + "&limit=500", "c3_limit500")
d3 = jload(r)
c3["limit=500"] = {"http": r.status_code, "resultCount": d3.get("resultCount"),
                   "n_results": len(d3.get("results", []))}
results["check3"] = c3

# ---------------------------------------------------------------- CHECK 4
print("\n=== CHECK 4: batch lookup limit ===")
c4 = {}
# Source a pool of real ids: expand the Quick 18 / Sagacity artist account.
r = get(
    "https://itunes.apple.com/lookup?id=433703118&entity=software&limit=200&country=us",
    "c4_artist_433703118_expansion",
)
d = jload(r)
res = d.get("results", [])
artist_row = [x for x in res if x.get("wrapperType") == "artist"]
apps = [x for x in res if x.get("wrapperType") == "software"]
pool = [x["trackId"] for x in apps if x.get("trackId")]
c4["artistId_expansion_433703118"] = {
    "resultCount": d.get("resultCount"),
    "artist_row": artist_row[0] if artist_row else None,
    "n_software": len(apps),
    "sample": [{"trackId": x.get("trackId"), "trackName": x.get("trackName"),
                "bundleId": x.get("bundleId")} for x in apps[:10]],
}
print(f"    pool size from artist expansion: {len(pool)}")

for n in (25, 100, 150, 200):
    if len(pool) < n:
        c4[f"batch_{n}"] = {"skipped": f"pool only has {len(pool)} ids"}
        continue
    ids = pool[:n]
    r = get(
        "https://itunes.apple.com/lookup?id=" + ",".join(str(i) for i in ids) + "&country=us",
        f"c4_batch_{n}",
    )
    try:
        d = jload(r)
    except Exception as e:
        c4[f"batch_{n}"] = {"http": r.status_code, "parse_error": str(e),
                            "body_head": r.text[:300]}
        continue
    got = [x.get("trackId") for x in d.get("results", []) if x.get("wrapperType") == "software"]
    c4[f"batch_{n}"] = {
        "http": r.status_code,
        "requested": n,
        "resultCount": d.get("resultCount"),
        "n_software_returned": len(got),
        "missing_ids": [i for i in ids if i not in set(got)],
        "url_len": len("https://itunes.apple.com/lookup?id=" + ",".join(str(i) for i in ids) + "&country=us"),
        "SILENT_TRUNCATION": len(got) < n,
    }
results["check4"] = c4
results["_pool_sample"] = pool[:200]

out = os.path.join(HERE, "..", "docs", "raw", "checks_1_4_summary.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2, ensure_ascii=False)
print("\nwrote", out)
print(json.dumps({k: v for k, v in results.items() if k != "_pool_sample"}, indent=2)[:6000])
