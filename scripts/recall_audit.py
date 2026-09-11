"""Recall audit: how many facilities we show as having NO app actually DO have one?

Draws a random sample of unlinked facilities, searches the live App Store directly
(not our swept corpus -- this measures whether the SWEEP itself is missing apps, not
just whether the JOIN failed to link something already in the corpus), and for any
genuine hit, runs it through the same vendor-labelling logic as the main pipeline.

Two search variants per facility (facility name alone, and name + city), because
Probe F (docs/findings.md) measured name-only search missing ~25% of KNOWN apps --
understating existence with a single query would bias this audit toward "we're
missing more than we are."

Every candidate result is filtered through a name-similarity check against the
facility before being counted as a real hit -- iTunes /search returns loosely
related golf apps for almost any golf-ish query, and counting those would make
coverage look worse than it is.
"""
import random
import re
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import config, db, itunes, join, vendors

SAMPLE_SIZE = 60
random.seed(20260818)  # reproducible

reg = vendors.load()
client = itunes.ITunesClient()

STOP = {"golf", "club", "course", "resort", "country", "links", "the", "at", "and",
        "gc", "cc", "of"}


def toks(s):
    return {t for t in re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split()
            if t and t not in STOP}


def name_sim(a, b):
    ta, tb = toks(a), toks(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


# ---------------------------------------------------------------- sample
unlinked = db.query(f"""
    SELECT f.facility_id, f.facility_name, f.city, f.state_code
    FROM facility f
    WHERE {config.FACILITY_WHERE}
      AND NOT EXISTS (SELECT 1 FROM ga_facility_app fa WHERE fa.facility_id = f.facility_id)
""")
sample = random.sample(unlinked, SAMPLE_SIZE)
print(f"sampled {len(sample)} of {len(unlinked)} unlinked facilities", flush=True)

known_track_ids = db.known_track_ids()

results = []
for i, f in enumerate(sample, 1):
    name, city, state = f["facility_name"], f["city"], f["state_code"]
    candidates = {}

    for term in (name, f"{name} {city}" if city else None):
        if not term:
            continue
        try:
            hits, _trunc = client.search(term)
        except Exception as e:
            print(f"  [{i}/{len(sample)}] search failed for {term!r}: {e}")
            continue
        for h in hits:
            tid = h.get("trackId")
            if tid and h.get("wrapperType") == "software":
                candidates[tid] = h

    # Keep only results that plausibly ARE this course (not just golf-flavored noise)
    best = None
    for tid, h in candidates.items():
        sim = name_sim(h.get("trackName"), name)
        looks_course = vendors.looks_like_course_app({
            "track_name": h.get("trackName"), "description": h.get("description")})
        if sim >= 0.4 and looks_course:
            if best is None or sim > best[1]:
                best = (h, sim)

    row = {"facility_id": f["facility_id"], "facility_name": name,
          "city": city, "state": state, "found_app": False}

    if best:
        h, sim = best
        tid = h.get("trackId")
        row.update({
            "found_app": True, "app_name": h.get("trackName"),
            "track_id": tid, "name_similarity": round(sim, 2),
            "already_in_our_corpus": tid in known_track_ids,
        })
        label = vendors.label_app(itunes.app_row(h), reg)
        row["vendor"] = label.vendor
        row["vendor_confidence"] = label.confidence

    results.append(row)
    tag = f"FOUND ({row.get('vendor') or 'unlabelled'})" if row["found_app"] else "no app"
    print(f"  [{i}/{len(sample)}] {name[:36]:38s} {str(city)[:16]:18s} -> {tag}")

# ---------------------------------------------------------------- report
found = [r for r in results if r["found_app"]]
in_corpus = [r for r in found if r["already_in_our_corpus"]]
not_in_corpus = [r for r in found if not r["already_in_our_corpus"]]

print("\n" + "=" * 74)
print("RECALL AUDIT")
print("=" * 74)
print(f"  sample size                         : {len(results)}")
print(f"  facilities where a real app was found: {len(found)} "
     f"({len(found)/len(results)*100:.1f}%)")
print(f"    ...already in our corpus (join miss): {len(in_corpus)}")
print(f"    ...NOT in our corpus (sweep miss)    : {len(not_in_corpus)}")
print()
print(f"  estimated true recall gap: ~{len(found)/len(results)*100:.0f}% of the "
     f"{len(unlinked):,} 'no app' facilities may actually have one")
print(f"  estimated additional apps out there: ~"
     f"{int(len(found)/len(results) * len(unlinked)):,}")

if found:
    print("\nvendor breakdown of found apps:")
    from collections import Counter
    c = Counter(r.get("vendor") or "unlabelled" for r in found)
    for v, n in c.most_common():
        disp = reg.vendors[v].display_name if v in reg.vendors else v
        print(f"    {disp:28s} {n}")

with open("docs/recall_audit.json", "w") as fp:
    json.dump(results, fp, indent=2, default=str)
print("\nwrote docs/recall_audit.json")
