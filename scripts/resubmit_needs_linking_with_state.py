"""Every app in the Needs Linking pool already went through join.py once and
came back No Match. Many of their titles carry a trailing state ("Westwood
Country Club TX", "Highlands Country Club (CA)") that the OLD candidate_name()
either sent along as noise (no state filter) or didn't strip at all -- fixed
now in join.py's strip_trailing_state(). Re-submitting just the apps where
state extraction actually changes the request should recover real matches,
same trusted match-service path everything else in "Looks Good" went through.

Dry run by default; pass --apply to write.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, join, config

APPLY = "--apply" in sys.argv

apps = db.query("""
    SELECT a.track_id, a.track_name, a.description FROM ga_app a
    JOIN ga_app_vendor v ON v.track_id = a.track_id AND v.vendor IS NOT NULL
    LEFT JOIN ga_facility_app fa ON fa.track_id = a.track_id
    LEFT JOIN app_crosswalk cw ON cw.track_id = a.track_id
    WHERE a.delisted_at IS NULL AND a.not_a_golf_course_at IS NULL
      AND a.canadian_course_at IS NULL AND fa.track_id IS NULL
      AND cw.track_id IS NULL
    ORDER BY a.track_id
""")
print(f"{len(apps)} needs-linking apps")

items, index = [], []
for a in apps:
    name, city, state = join.candidate_name(a)
    if not name or len(name) < 3 or not state:
        continue
    code = join.normalize_state(state)
    if not code:
        continue
    item = {"facility_name": name, "state": code}
    if city:
        item["city"] = city
    items.append(item)
    index.append(a["track_id"])

print(f"{len(items)} have a usable state hint -- submitting to match-service")

in_scope = {r["facility_id"] for r in db.query(
    f"SELECT facility_id FROM facility WHERE {config.FACILITY_WHERE}")}

links, unmatched = [], []
for i in range(0, len(items), config.MATCH_BATCH_SIZE):
    chunk = items[i:i + config.MATCH_BATCH_SIZE]
    ids = index[i:i + config.MATCH_BATCH_SIZE]
    job = join._submit(chunk, config.MATCH_CONFIDENCE_THRESHOLD)
    print(f"  batch {i // config.MATCH_BATCH_SIZE}: {len(chunk)} items, job={job}")
    results = join._poll(job, total=len(chunk))
    if len(results) != len(chunk):
        raise RuntimeError(f"positional mismatch: {len(results)} vs {len(chunk)}")
    for track_id, req, res in zip(ids, chunk, results):
        status = res.get("matchStatus") or "unknown"
        fid = res.get("facility_id")
        if fid and int(fid) not in in_scope:
            fid = None
        if fid:
            links.append({"facility_id": int(fid), "track_id": track_id,
                          "match_confidence": res.get("confidence"),
                          "match_method": "match_service", "match_status": status,
                          "submitted": req})
        else:
            unmatched.append({"track_id": track_id, "submitted": req, "status": status})

print(f"\nmatched: {len(links)}  unmatched: {len(unmatched)}")
by_status = {}
for u in unmatched:
    by_status[u["status"]] = by_status.get(u["status"], 0) + 1
print("unmatched by status:", by_status)

with open("docs/resubmit_needs_linking_results.json", "w") as f:
    json.dump({"links": links, "unmatched": unmatched}, f, indent=2, default=str)
print("wrote docs/resubmit_needs_linking_results.json")

if APPLY and links:
    touched = sorted({l["track_id"] for l in links})
    for i in range(0, len(touched), 400):
        chunk = ",".join(str(t) for t in touched[i:i + 400])
        db.exec_sql(f"DELETE FROM ga_facility_app WHERE match_method='match_service' "
                   f"AND track_id IN ({chunk})")
    write_rows = [{k: v for k, v in l.items() if k != "submitted"} for l in links]
    db.upsert("ga_facility_app", write_rows, on_conflict="facility_id,track_id")
    print(f"\napplied {len(write_rows)} links")
else:
    print("\nDRY RUN -- nothing written. Re-run with --apply to write.")
