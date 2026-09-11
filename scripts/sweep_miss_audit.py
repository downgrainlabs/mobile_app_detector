"""How many UNIQUE apps, from real vendors, is the SWEEP missing entirely?

Distinct from recall_audit.py's facility-level question ("does this facility have an
app we're not showing"), which conflated two different failure modes: apps already
in our corpus that just never got linked to a facility (a JOIN problem, already
being worked via the resolve/verify queues), versus apps the sweep never found at
all (a genuine coverage gap, only fixable by sweeping more).

This script isolates the second number specifically, at N=500 for real statistical
power: search live for each sampled unlinked facility, keep only candidates that
(a) plausibly ARE that specific course by name, (b) are geography-consistent (an app
explicitly naming a different state is a same-name collision, not a real hit -- this
exact bug produced 3 false positives in the N=60 pilot: "Northfield Golf Club - MN"
matched against a Massachusetts facility), (c) look like a genuine single-course
vendor app rather than a consumer tool/aggregator/tour app, and (d) are NOT already
a track_id in ga_app. Then dedupe to unique missed apps and label their vendor.
"""
import random
import re
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import config, db, itunes, vendors

SAMPLE_SIZE = 500
random.seed(20260818)

reg = vendors.load()
client = itunes.ITunesClient()
known_track_ids = db.known_track_ids()

STOP = {"golf", "club", "course", "resort", "country", "links", "the", "at", "and",
        "gc", "cc", "of"}

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
_STATE_RE = {code: re.compile(rf"(?i)\b({re.escape(full)}|{code})\b")
            for code, full in STATE_NAMES.items()}


def toks(s):
    return {t for t in re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split()
            if t and t not in STOP}


def name_sim(a, b):
    ta, tb = toks(a), toks(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def mentioned_states(app_row):
    blob = " ".join(str(app_row.get(k) or "") for k in
                    ("trackName", "description", "sellerName", "artistName"))
    return {code for code, rx in _STATE_RE.items() if rx.search(blob)}


# ---------------------------------------------------------------- sample
unlinked = db.query(f"""
    SELECT f.facility_id, f.facility_name, f.city, f.state_code
    FROM facility f
    WHERE {config.FACILITY_WHERE}
      AND NOT EXISTS (SELECT 1 FROM ga_facility_app fa WHERE fa.facility_id = f.facility_id)
""")
sample = random.sample(unlinked, min(SAMPLE_SIZE, len(unlinked)))
print(f"sampled {len(sample)} of {len(unlinked)} unlinked facilities", flush=True)

missed_apps = {}          # track_id -> {app data, matched_facilities: [...]}
facilities_with_a_hit = 0
rejected_geo = 0
rejected_not_course = 0

for i, f in enumerate(sample, 1):
    name, city, state = f["facility_name"], f["city"], f["state_code"]
    candidates = {}
    for term in (name, f"{name} {city}" if city else None):
        if not term:
            continue
        try:
            hits, _trunc = client.search(term)
        except Exception as e:
            print(f"  [{i}/{len(sample)}] search failed {term!r}: {e}")
            continue
        for h in hits:
            tid = h.get("trackId")
            if tid and h.get("wrapperType") == "software":
                candidates[tid] = h

    hit_this_facility = False
    for tid, h in candidates.items():
        if tid in known_track_ids:
            continue  # already in our sweep -- not a sweep miss, regardless of link state
        sim = name_sim(h.get("trackName"), name)
        if sim < 0.4:
            continue
        row = {"track_name": h.get("trackName"), "description": h.get("description"),
              "sellerName": h.get("sellerName"), "artistName": h.get("artistName")}
        if not vendors.looks_like_course_app(
            {"track_name": h.get("trackName"), "description": h.get("description")}
        ):
            rejected_not_course += 1
            continue
        m_states = mentioned_states(h)
        if m_states and state not in m_states:
            rejected_geo += 1
            continue

        hit_this_facility = True
        entry = missed_apps.setdefault(tid, {
            "track_id": tid, "app_name": h.get("trackName"),
            "seller_name": h.get("sellerName"), "artist_name": h.get("artistName"),
            "matched_facilities": [],
        })
        entry["matched_facilities"].append({
            "facility_id": f["facility_id"], "facility_name": name,
            "city": city, "state": state, "name_sim": round(sim, 2),
        })

    if hit_this_facility:
        facilities_with_a_hit += 1

    if i % 25 == 0:
        print(f"  [{i}/{len(sample)}] ... {len(missed_apps)} unique missed apps "
             f"found so far ({facilities_with_a_hit} facilities with a hit)",
             flush=True)

# ---------------------------------------------------------------- vendor-label the misses
for tid, entry in missed_apps.items():
    row = {"track_id": tid, "track_name": entry["app_name"],
          "seller_name": entry["seller_name"], "artist_name": entry["artist_name"],
          "description": None}
    label = vendors.label_app(row, reg)
    entry["vendor"] = label.vendor
    entry["vendor_confidence"] = label.confidence

# ---------------------------------------------------------------- report
print("\n" + "=" * 74)
print("SWEEP-MISS AUDIT")
print("=" * 74)
print(f"  facilities searched                       : {len(sample)}")
print(f"  facilities with >=1 genuine hit            : {facilities_with_a_hit} "
     f"({facilities_with_a_hit/len(sample)*100:.1f}%)")
print(f"  rejected as non-course-app (games/tools)   : {rejected_not_course}")
print(f"  rejected as geography-inconsistent         : {rejected_geo}")
print()
print(f"  UNIQUE apps missing from the sweep         : {len(missed_apps)}")
print(f"  -> that's {len(missed_apps)/len(sample)*100:.1f} unique missed apps per "
     f"100 facilities searched")

rate = len(missed_apps) / len(sample)
print(f"\n  extrapolated to all {len(unlinked):,} unlinked facilities: "
     f"~{int(rate * len(unlinked)):,} unique apps likely still unswept")
print("  (this is an upper-bound-ish estimate: multiple facilities can share one")
print("   missed app, so the true unique-app extrapolation is somewhat lower; the")
print("   facility-level hit rate above is the more defensible top-line number)")

from collections import Counter
vc = Counter(e["vendor"] or "unlabelled" for e in missed_apps.values())
print("\nvendor breakdown of missed apps:")
for v, n in vc.most_common():
    disp = reg.vendors[v].display_name if v in reg.vendors else v
    print(f"    {disp:28s} {n}")

with open("docs/sweep_miss_audit.json", "w") as fp:
    json.dump({
        "sample_size": len(sample),
        "facilities_with_hit": facilities_with_a_hit,
        "unique_missed_apps": len(missed_apps),
        "rejected_not_course_app": rejected_not_course,
        "rejected_geo_inconsistent": rejected_geo,
        "missed_apps": list(missed_apps.values()),
    }, fp, indent=2, default=str)
print("\nwrote docs/sweep_miss_audit.json")
