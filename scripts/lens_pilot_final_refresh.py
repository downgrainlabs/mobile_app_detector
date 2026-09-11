"""Final refresh of the 80-app Lens pilot review file:
  1. Re-run Stage B for any app whose extract_city_state() result changed
     with the rank-order fix (no new Lens cost -- cached raw data only).
  2. Purge any cached result for an app likely_non_us() flags (should never
     have been attempted -- e.g. River Bend Golf Club, Canadian).
  3. Flag any app that's since been linked through a DIFFERENT channel since
     the sample was drawn (e.g. Apple Creek Golf Club, resolved via the
     ClubCaddie OCR fix after this pilot's sample was taken) -- these are
     stale "No Match" rows that don't need review anymore.
Still apply_links=False. Nothing written to ga_facility_app.
"""
import sys, os, csv, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, join, config, lens

sample = json.load(open("docs/lens_pilot_sample.json", encoding="utf-8"))
all_ids = sample["residue_sample"] + sample["needs_linking_sample"]
source = {tid: "collision_residue" for tid in sample["residue_sample"]}
source.update({tid: "needs_linking" for tid in sample["needs_linking_sample"]})

apps = {r["track_id"]: r for r in db.query(
    f"SELECT track_id, track_name, subtitle, seller_url, support_url, "
    f"privacy_policy_url, developer_website FROM ga_app WHERE track_id IN "
    f"({','.join(str(t) for t in all_ids)})"
)}

# ---- step 2: purge non-US apps' cached results ------------------------------
non_us = {tid: lens.likely_non_us(a) for tid, a in apps.items() if lens.likely_non_us(a)}
if non_us:
    ids = ",".join(str(t) for t in non_us)
    db.exec_sql(f"UPDATE ga_lens_detection SET matcher_facility_id=NULL, "
               f"matcher_status='Skipped -- non-US' WHERE track_id IN ({ids})")
print(f"purged {len(non_us)} non-US apps' cached results: {non_us}")

# ---- step 1: re-run Stage B for any changed extraction ----------------------
cached = {r["track_id"]: r for r in db.query(
    f"SELECT track_id, query_used, extracted_city, extracted_state, raw "
    f"FROM ga_lens_detection WHERE track_id IN ({','.join(str(t) for t in all_ids)})"
)}
changed_ids, new_extract = [], {}
for tid, r in cached.items():
    if tid in non_us:
        continue
    city, state = lens.extract_city_state(r["raw"] or {})
    new_extract[tid] = (city, state)
    if (city, state) != (r["extracted_city"], r["extracted_state"]):
        changed_ids.append(tid)
print(f"{len(changed_ids)} apps need Stage B re-run")

in_scope = {r["facility_id"] for r in db.query(
    f"SELECT facility_id FROM facility WHERE {config.FACILITY_WHERE}")}
if changed_ids:
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
    job = join._submit(items, config.MATCH_CONFIDENCE_THRESHOLD)
    results = join._poll(job, total=len(items))
    records = []
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
    print(f"updated {len(records)} ga_lens_detection rows")

# ---- step 3: check ALL 80 for a current link (not just residue) ------------
live_links = {r["track_id"]: r for r in db.query(f"""
    SELECT fa.track_id, fa.facility_id, fa.match_method, f.facility_name, f.city, f.state_code
    FROM ga_facility_app fa JOIN facility f ON f.facility_id = fa.facility_id
    WHERE fa.track_id IN ({','.join(str(t) for t in all_ids)})
""")}
print(f"{len(live_links)} of the 80 currently have SOME link (residue: had one at "
     f"sample time; needs_linking: resolved by a different channel since)")

# ---- rebuild the review CSV --------------------------------------------------
rows = db.query(f"""
    SELECT l.track_id, a.track_name, l.query_used, l.extracted_city,
           l.extracted_state, l.matcher_status, l.matcher_facility_id,
           f.facility_name, f.city, f.state_code
    FROM ga_lens_detection l
    JOIN ga_app a ON a.track_id = l.track_id
    LEFT JOIN facility f ON f.facility_id = l.matcher_facility_id
    WHERE l.track_id IN ({','.join(str(t) for t in all_ids)})
""")

out = []
for r in rows:
    tid = r["track_id"]
    cs = (f"{r['extracted_city'] or ''}, {r['extracted_state'] or ''}"
         if (r["extracted_city"] or r["extracted_state"]) else "")
    prop = (f"#{r['matcher_facility_id']} {r['facility_name']} | "
           f"{r['city']}, {r['state_code']}") if r["matcher_facility_id"] else ""
    live = live_links.get(tid)
    live_label = (f"#{live['facility_id']} {live['facility_name']} | "
                 f"{live['city']}, {live['state_code']} (via {live['match_method']})"
                 ) if live else ""
    out.append({
        "source_population": source.get(tid, ""),
        "track_id": tid, "app_name": r["track_name"],
        "app_store_url": f"https://apps.apple.com/us/app/id{tid}",
        "query_used": r["query_used"] or "", "extracted_city_state": cs,
        "matcher_status": r["matcher_status"] or "(no Lens result)",
        "lens_proposed_facility": prop,
        "current_link": live_label,
        "already_resolved_since_sampled": "y" if (tid in live_links and source.get(tid) == "needs_linking") else "",
        "non_us_skip": non_us.get(tid, ""),
        "CORRECT [y/n]": "", "ACTUAL_FACILITY_ID": "", "NOTES": "",
    })

out.sort(key=lambda r: (r["source_population"], r["track_id"]))
HEADER = ["source_population", "track_id", "app_name", "app_store_url",
          "query_used", "extracted_city_state", "matcher_status",
          "lens_proposed_facility", "current_link",
          "already_resolved_since_sampled", "non_us_skip",
          "CORRECT [y/n]", "ACTUAL_FACILITY_ID", "NOTES"]
path = "docs/lens_pilot_review_v5.csv"
with open(path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=HEADER)
    w.writeheader()
    w.writerows(out)
print(f"wrote {path}: {len(out)} rows")
