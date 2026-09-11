"""Re-run Stage B (match-service only, zero new Lens cost) for every pilot app
whose extract_city_state() result changes now that GC/CC/invalid state codes
and club-name-echoed 'cities' are rejected. Rebuilds the pilot review CSV so
it reflects real evidence, not the pre-fix garbage. Still apply_links=False --
this remains Derek's review file, nothing gets written to ga_facility_app.
"""
import sys, os, csv, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, join, config, lens

sample = json.load(open("docs/lens_pilot_sample.json", encoding="utf-8"))
all_ids = sample["residue_sample"] + sample["needs_linking_sample"]
source = {tid: "collision_residue" for tid in sample["residue_sample"]}
source.update({tid: "needs_linking" for tid in sample["needs_linking_sample"]})

cached = {r["track_id"]: r for r in db.query(
    f"SELECT track_id, query_used, extracted_city, extracted_state, raw "
    f"FROM ga_lens_detection WHERE track_id IN ({','.join(str(t) for t in all_ids)})"
)}

changed_ids = []
new_extract = {}
for tid, r in cached.items():
    city, state = lens.extract_city_state(r["raw"] or {})
    new_extract[tid] = (city, state)
    if (city, state) != (r["extracted_city"], r["extracted_state"]):
        changed_ids.append(tid)

print(f"{len(changed_ids)} apps need Stage B re-run with corrected extraction")

in_scope = {r["facility_id"] for r in db.query(
    f"SELECT facility_id FROM facility WHERE {config.FACILITY_WHERE}")}

items, ids = [], []
for tid in changed_ids:
    query = cached[tid]["query_used"]
    city, state = new_extract[tid]
    item = {"facility_name": query}
    if city:
        item["city"] = city
    code = join.normalize_state(state) if state else None
    if code:
        item["state"] = code
    items.append(item)
    ids.append(tid)

records = []
if items:
    job = join._submit(items, config.MATCH_CONFIDENCE_THRESHOLD)
    results = join._poll(job, total=len(items))
    for tid, res in zip(ids, results):
        status = res.get("matchStatus") or "unknown"
        fid = res.get("facility_id")
        rejected = fid and int(fid) not in in_scope
        if rejected:
            fid = None
        city, state = new_extract[tid]
        records.append({
            "track_id": tid, "query_used": cached[tid]["query_used"],
            "extracted_city": city, "extracted_state": state,
            "matcher_facility_id": int(fid) if fid else None,
            "matcher_status": status + (" (rejected: out of scope)" if rejected else ""),
            "matcher_confidence": res.get("confidence"),
        })
    db.upsert("ga_lens_detection", records, on_conflict="track_id")
    print(f"updated {len(records)} ga_lens_detection rows with corrected Stage B results")

# ---- rebuild the full 80-row review CSV from current (corrected) cache ------
rows = db.query(f"""
    SELECT l.track_id, a.track_name, l.query_used, l.extracted_city,
           l.extracted_state, l.matcher_status, l.matcher_facility_id,
           f.facility_name, f.city, f.state_code
    FROM ga_lens_detection l
    JOIN ga_app a ON a.track_id = l.track_id
    LEFT JOIN facility f ON f.facility_id = l.matcher_facility_id
    WHERE l.track_id IN ({','.join(str(t) for t in all_ids)})
""")

live_links = {}
for r in db.query(f"""
    SELECT fa.track_id, fa.facility_id, f.facility_name, f.city, f.state_code
    FROM ga_facility_app fa JOIN facility f ON f.facility_id = fa.facility_id
    WHERE fa.track_id IN ({','.join(str(t) for t in sample['residue_sample'])})
""" if sample["residue_sample"] else "SELECT 1 WHERE false"):
    live_links[r["track_id"]] = r

out = []
for r in rows:
    tid = r["track_id"]
    cs = (f"{r['extracted_city'] or ''}, {r['extracted_state'] or ''}"
         if (r["extracted_city"] or r["extracted_state"]) else "")
    prop = (f"#{r['matcher_facility_id']} {r['facility_name']} | "
           f"{r['city']}, {r['state_code']}") if r["matcher_facility_id"] else ""
    live = live_links.get(tid)
    live_label = (f"#{live['facility_id']} {live['facility_name']} | "
                 f"{live['city']}, {live['state_code']}") if live else ""
    out.append({
        "source_population": source.get(tid, ""),
        "track_id": tid, "app_name": r["track_name"],
        "app_store_url": f"https://apps.apple.com/us/app/id{tid}",
        "query_used": r["query_used"] or "", "extracted_city_state": cs,
        "matcher_status": r["matcher_status"] or "(no Lens result)",
        "lens_proposed_facility": prop,
        "current_residue_link": live_label,
        "was_corrected": "y" if tid in changed_ids else "",
        "CORRECT [y/n]": "", "ACTUAL_FACILITY_ID": "", "NOTES": "",
    })

out.sort(key=lambda r: (r["source_population"], r["track_id"]))
HEADER = ["source_population", "track_id", "app_name", "app_store_url",
          "query_used", "extracted_city_state", "matcher_status",
          "lens_proposed_facility", "current_residue_link", "was_corrected",
          "CORRECT [y/n]", "ACTUAL_FACILITY_ID", "NOTES"]
path = "docs/lens_pilot_review_v4.csv"
with open(path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=HEADER)
    w.writeheader()
    w.writerows(out)

print(f"wrote {path}: {len(out)} rows ({len(changed_ids)} marked was_corrected)")
