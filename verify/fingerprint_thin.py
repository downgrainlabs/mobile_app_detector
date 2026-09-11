"""Targeted follow-up on the five priority vendors with thin/ambiguous coverage.

Hypothesis under test: some vendors do NOT ship per-club white-label apps at all —
they ship ONE multi-tenant app the member logs into. If true, those vendors' courses
are structurally invisible to a facility->app->vendor pipeline, which is a very
different problem from "we failed to find them".
"""
import json
import os
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
DELAY = 2.5
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
        back = min(240, 30 * (2 ** a))
        print(f"      {r.status_code}; backoff {back}s")
        time.sleep(back)
    return {"results": []}


PROBES = {
    "northstar_sibisoft": {
        "tok": ["sibisoft", "northstar club", "globalnorthstar"],
        "queries": ["sibisoft", "northstar club management", "sibisoft club",
                    "northstar clubs"],
    },
    "foretees": {
        "tok": ["foretees", "3embed.foretess"],
        "queries": ["foretees", "clubcentral foretees", "foretees club"],
    },
    "buzclub": {
        "tok": ["buz club", "buzsoftware", "buzclub"],
        "queries": ["buz club software", "buz software club", "thebuz club"],
    },
    "whoosh": {
        "tok": ["whoosh"],
        "queries": ["whoosh member", "whoosh golf club", "whoosh clubhouse",
                    "whoosh pro shop"],
    },
    "clubprophet": {
        "tok": ["clubprophet", "club prophet"],
        "queries": ["club prophet systems", "clubprophet golf", "cps golf"],
    },
}
FIELDS = ["bundleId", "sellerUrl", "artistName", "sellerName", "trackName", "description"]
out = {}

for name, cfg in PROBES.items():
    hits = {}
    for q in cfg["queries"]:
        d = jget("https://itunes.apple.com/search?term=" + urllib.parse.quote_plus(q)
                 + "&entity=software&country=us&limit=200")
        for x in d.get("results", []):
            blob = " ".join(str(x.get(f) or "") for f in FIELDS).lower()
            if any(t in blob for t in cfg["tok"]):
                hits[x["trackId"]] = x
        print(f"  {name:20s} {q!r:32s} -> cumulative {len(hits)}")

    # expand the dominant artist account: does the vendor publish a fleet?
    counts = defaultdict(int)
    for x in hits.values():
        if x.get("artistId"):
            counts[x["artistId"]] += 1
    expanded = {}
    for aid, n in sorted(counts.items(), key=lambda kv: -kv[1])[:2]:
        d = jget(f"https://itunes.apple.com/lookup?id={aid}&entity=software"
                 f"&limit=200&country=us")
        apps = [x for x in d.get("results", []) if x.get("wrapperType") == "software"]
        expanded[aid] = {"artist_held_apps": len(apps),
                         "names": [x.get("trackName") for x in apps[:12]]}
        print(f"  {name:20s} artistId {aid} publishes {len(apps)} apps")

    out[name] = {
        "n_matched": len(hits),
        "apps": [{"trackId": k, "name": x.get("trackName"),
                  "bundleId": x.get("bundleId"), "artistName": x.get("artistName"),
                  "artistId": x.get("artistId"), "sellerUrl": x.get("sellerUrl"),
                  "desc_head": (x.get("description") or "")[:110]}
                 for k, x in hits.items()],
        "artist_expansion": expanded,
    }

print("\n" + "=" * 78)
for name, r in out.items():
    print(f"\n{name.upper()}  matched={r['n_matched']}")
    for a in r["apps"][:8]:
        print(f"   {str(a['name'])[:36]:38s} {str(a['bundleId'])[:34]:36s} "
              f"{str(a['artistName'])[:22]}")
    for aid, e in r["artist_expansion"].items():
        print(f"   artist {aid} -> {e['artist_held_apps']} apps: "
              f"{', '.join(str(n)[:22] for n in e['names'][:5])}")

with open(os.path.join(RAW, "vendor_thin_probe.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print("\nwrote docs/raw/vendor_thin_probe.json")
