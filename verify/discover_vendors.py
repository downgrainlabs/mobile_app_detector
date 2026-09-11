"""Offline: discover candidate vendors from the corpus itself.

White-label vendors betray themselves by REPETITION — the same bundleId prefix or
the same sellerUrl domain across many otherwise-unrelated courses. So cluster the
corpus on those two fields and rank by how many distinct courses share each one.
No vendor list required as input; the residue names its own vendors.
"""
import json
import os
import re
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "..", "docs", "raw")
rows = json.load(open(os.path.join(RAW, "pool_rows.json"), encoding="utf-8"))

KNOWN = ["sagacity", "quick18", "gallus", "chronogolf", "foreup", "teesnap",
         "clubprophet", "club prophet", "golfnow", "lightspeed"]

BOOK = re.compile(r"(?i)(tee time|book .{0,25}tee|tee sheet|golf reservation)")
COURSEY = re.compile(r"(?i)(provides tee time booking|book(ing)? tee times? (for|at)|"
                     r"golf (course|club|resort)|country club)")


def domain(u):
    m = re.match(r"https?://([^/]+)", (u or "").strip(), re.I)
    if not m:
        return None
    h = m.group(1).lower().lstrip("www.")
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def prefix(b):
    """Vendor-ish bundle prefix: first two components (com.gallusgolf)."""
    if not b:
        return None
    p = b.lower().split(".")
    return ".".join(p[:2]) if len(p) >= 2 else b.lower()


by_prefix = defaultdict(list)
by_domain = defaultdict(list)
n_book = 0
for tid, x in rows.items():
    blob = " ".join(str(x.get(k) or "") for k in ("trackName", "description"))
    if not (BOOK.search(blob) and COURSEY.search(blob)):
        continue
    n_book += 1
    p = prefix(x.get("bundleId"))
    d = domain(x.get("sellerUrl"))
    entry = {"trackId": tid, "name": x.get("trackName"),
             "artist": x.get("artistName"), "bundle": x.get("bundleId"),
             "sellerUrl": x.get("sellerUrl")}
    if p:
        by_prefix[p].append(entry)
    if d:
        by_domain[d].append(entry)


def is_known(s):
    return any(k in (s or "").lower().replace(" ", "") for k in
               [k.replace(" ", "") for k in KNOWN])


def show(title, groups, minimum, skip_generic=()):
    print(f"\n{'='*74}\n{title}\n{'='*74}")
    out = []
    for key, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        # distinct artists => genuinely multi-operator, i.e. white-label
        artists = {i["artist"] for i in items}
        if len(items) < minimum or key in skip_generic:
            continue
        flag = "KNOWN" if is_known(key) else "**NEW**"
        out.append({"key": key, "apps": len(items), "distinct_operators": len(artists),
                    "status": flag,
                    "examples": [i["name"] for i in items[:3]],
                    "example_artists": sorted(artists)[:3]})
        print(f"{flag:8s} {key:34s} apps={len(items):4d}  operators={len(artists):4d}  "
              f"e.g. {', '.join(str(i['name'])[:26] for i in items[:2])}")
    return out


print(f"corpus: {len(rows)} apps, {n_book} classified as course booking apps")

res = {}
# A shared bundle prefix across MANY operators is the strongest white-label tell.
res["bundle_prefix_clusters"] = show(
    "BUNDLE PREFIX shared across courses  (>=2 apps)", by_prefix, 2)
# Generic personal-dev prefixes are noise, not vendors.
res["seller_domain_clusters"] = show(
    "sellerUrl DOMAIN shared across courses  (>=2 apps)", by_domain, 2)

with open(os.path.join(RAW, "vendor_discovery.json"), "w", encoding="utf-8") as f:
    json.dump(res, f, indent=2, ensure_ascii=False)
print("\nwrote docs/raw/vendor_discovery.json")
