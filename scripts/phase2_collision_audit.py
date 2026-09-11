"""Audit the 2,049 original Phase 2 (match_service) links for the collision problem
found repeatedly today (Ridgewood, Arrowhead, Rolling Meadows-KS): match-service can
silently pick one of several real, equally-plausible facilities and return
confidence=1.0 either way. These links predate any of today's collision-detection
work and were never checked -- only 140 have candidate data stored at all.

Free to run: match-service-2-0 is unlimited, unlike the Lens API. Re-derives each
app's query the same way the original join did (join.candidate_name), resubmits with
include_candidates=True, and classifies every live link into:

  DISAGREEMENT  -- fresh resubmission's top pick is a DIFFERENT facility than what's
                   live. Strongest signal something is wrong; check these first.
  COLLISION     -- top pick agrees with what's live, but >=1 other in-scope Golf
                   Course facility also came back as a real candidate. Not proof of
                   error, but exactly the shape of the Ridgewood/Arrowhead cases.
  CONFIRMED     -- agrees, and no other in-scope candidate exists. Trustworthy.
  VANISHED      -- re-submission returns No Match / Multiple Matches / empty entirely
                   for a query that used to resolve. Worth a look but lower priority
                   (could just mean match-service's index shifted, not that the
                   original link was wrong).
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import config, db, join

in_scope = {r["facility_id"] for r in db.query(
    f"SELECT facility_id FROM facility WHERE {config.FACILITY_WHERE}")}

rows = db.query("""
    SELECT fa.track_id, fa.facility_id AS live_facility_id, fa.match_status AS live_status,
           a.track_name, a.description
    FROM ga_facility_app fa
    JOIN ga_app a ON a.track_id = fa.track_id
    WHERE fa.match_method = 'match_service'
    ORDER BY fa.track_id
""")
print(f"auditing {len(rows)} original Phase 2 links", flush=True)

items, meta = [], []
for r in rows:
    name, city, state = join.candidate_name(
        {"track_name": r["track_name"], "description": r["description"]})
    if not name:
        name = r["track_name"]
    item = {"facility_name": name}
    if city:
        item["city"] = city
    code = join.normalize_state(state)
    if code:
        item["state"] = code
    items.append(item)
    meta.append(r)

print(f"submitting {len(items)} queries in batches of {config.MATCH_BATCH_SIZE}", flush=True)

all_results = []
for i in range(0, len(items), config.MATCH_BATCH_SIZE):
    chunk = items[i:i + config.MATCH_BATCH_SIZE]
    job = join._submit(chunk, config.MATCH_CONFIDENCE_THRESHOLD)
    res = join._poll(job, total=len(chunk))
    assert len(res) == len(chunk), f"batch {i}: got {len(res)} for {len(chunk)}"
    n_matched = sum(1 for x in res if x.get("facility_id"))
    if n_matched < len(chunk) * 0.10:
        raise RuntimeError(
            f"batch {i}: only {n_matched}/{len(chunk)} matched -- looks like a "
            f"transient match-service failure. Refusing to trust this run.")
    all_results.extend(res)
    print(f"  batch {i}-{i+len(chunk)}: {n_matched}/{len(chunk)} matched", flush=True)

assert len(all_results) == len(rows)

disagreements, collisions, confirmed, vanished = [], [], [], []
for r, item, res in zip(meta, items, all_results):
    fid = res.get("facility_id")
    cands = res.get("candidates") or []
    other_real = [c for c in cands
                  if c.get("facility_id") and int(c["facility_id"]) != r["live_facility_id"]
                  and int(c["facility_id"]) in in_scope
                  and c.get("facility_type") == "Golf Course"]

    rec = {"track_id": r["track_id"], "app_name": r["track_name"],
          "live_facility_id": r["live_facility_id"], "live_status": r["live_status"],
          "query_used": item, "fresh_status": res.get("matchStatus"),
          "fresh_facility_id": fid, "fresh_confidence": res.get("confidence"),
          "other_candidates": other_real}

    if not fid:
        vanished.append(rec)
    elif int(fid) != r["live_facility_id"]:
        disagreements.append(rec)
    elif other_real:
        collisions.append(rec)
    else:
        confirmed.append(rec)

print("\n" + "=" * 74)
print("PHASE 2 COLLISION AUDIT")
print("=" * 74)
print(f"  total links audited      : {len(rows)}")
print(f"  CONFIRMED (no issue)     : {len(confirmed)} ({len(confirmed)/len(rows)*100:.1f}%)")
print(f"  COLLISION (2nd candidate): {len(collisions)} ({len(collisions)/len(rows)*100:.1f}%)")
print(f"  DISAGREEMENT (top changed): {len(disagreements)} ({len(disagreements)/len(rows)*100:.1f}%)")
print(f"  VANISHED (no match now)  : {len(vanished)} ({len(vanished)/len(rows)*100:.1f}%)")

with open("docs/phase2_collision_audit.json", "w") as f:
    json.dump({
        "total": len(rows), "confirmed": len(confirmed), "collisions": collisions,
        "disagreements": disagreements, "vanished": vanished,
    }, f, indent=2, default=str)
print("\nwrote docs/phase2_collision_audit.json")
