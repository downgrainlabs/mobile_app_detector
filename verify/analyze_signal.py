"""Offline: how many golf BOOKING apps are vendor-labelled by the free iTunes
JSON alone? Bears directly on whether Phase 3 must fetch HTML for every candidate."""
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "..", "docs", "raw")
rows = json.load(open(os.path.join(RAW, "pool_rows.json"), encoding="utf-8"))

VENDORS = {
    "sagacity":    {"tok": ["sagacity", "quick18", "quick 18"], "bundle": ["com.quick18."]},
    "gallus":      {"tok": ["gallus"], "bundle": ["com.gallusgolf."]},
    "chronogolf":  {"tok": ["chronogolf"], "bundle": ["com.chronogolf."]},
    "lightspeed":  {"tok": ["lightspeed"], "bundle": ["com.lightspeed"]},
    "foreup":      {"tok": ["foreup"], "bundle": ["com.foreup"]},
    "teesnap":     {"tok": ["teesnap"], "bundle": ["com.teesnap"]},
    "clubprophet": {"tok": ["club prophet", "clubprophet"], "bundle": ["com.clubprophet"]},
    "golfnow":     {"tok": ["golfnow"], "bundle": ["com.golfnow"]},
    "clubcaddie":  {"tok": ["club caddie", "clubcaddie"], "bundle": ["com.clubcaddie"]},
    "jonas":       {"tok": ["jonas club"], "bundle": ["com.jonasclub"]},
}
FIELDS = ["sellerUrl", "bundleId", "artistName", "sellerName", "trackName", "description"]


def signals(x):
    hits = {}
    for v, cfg in VENDORS.items():
        for f in FIELDS:
            val = (x.get(f) or "").lower()
            if not val:
                continue
            if any(t in val for t in cfg["tok"]) or \
               (f == "bundleId" and any(val.startswith(b) for b in cfg["bundle"])):
                hits.setdefault(v, set()).add(f)
    return {k: sorted(v) for k, v in hits.items()}


# A white-label course booking app: description names tee-time booking for a course.
BOOK = re.compile(r"(?i)(tee time|book .{0,25}tee|tee sheet|golf reservation)")
COURSEY = re.compile(r"(?i)(provides tee time booking|book(ing)? tee times? (for|at)|"
                     r"golf (course|club|resort)|country club)")

tot = book = labelled = 0
by_vendor, by_field, only = {}, {}, {}
unlabelled = []
for tid, x in rows.items():
    tot += 1
    blob = " ".join(str(x.get(k) or "") for k in ("trackName", "description"))
    if not (BOOK.search(blob) and COURSEY.search(blob)):
        continue
    book += 1
    h = signals(x)
    if h:
        labelled += 1
        allf = set()
        for v, fs in h.items():
            by_vendor[v] = by_vendor.get(v, 0) + 1
            allf |= set(fs)
        for f in allf:
            by_field[f] = by_field.get(f, 0) + 1
        if len(allf) == 1:
            only[next(iter(allf))] = only.get(next(iter(allf)), 0) + 1
    else:
        unlabelled.append({"trackId": tid, "trackName": x.get("trackName"),
                           "artistName": x.get("artistName"),
                           "bundleId": x.get("bundleId"),
                           "sellerUrl": x.get("sellerUrl")})

out = {
    "pool_total_apps": tot,
    "classified_as_course_booking_apps": book,
    "labelled_by_free_json": labelled,
    "pct_labelled": round(labelled / book * 100, 1) if book else 0,
    "unlabelled": len(unlabelled),
    "by_vendor": dict(sorted(by_vendor.items(), key=lambda kv: -kv[1])),
    "by_field_any": dict(sorted(by_field.items(), key=lambda kv: -kv[1])),
    "labelled_by_exactly_one_field": dict(sorted(only.items(), key=lambda kv: -kv[1])),
    "unlabelled_sample": unlabelled[:30],
}
print(json.dumps({k: v for k, v in out.items() if k != "unlabelled_sample"}, indent=2))
print("\n--- sample of booking apps the free JSON could NOT label ---")
for u in unlabelled[:18]:
    print(f"  {str(u['trackName'])[:38]:40s} | {str(u['artistName'])[:26]:28s} | "
          f"{str(u['bundleId'])[:34]:36s} | {u['sellerUrl']}")

with open(os.path.join(RAW, "signal_coverage.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print("\nwrote docs/raw/signal_coverage.json")
