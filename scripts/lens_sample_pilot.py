"""Pilot: run the two-stage Lens+matcher pipeline (lens.py) against a random
40-app sample each from Collision Residue and Needs Linking -- both
populations where automated resolution (match-service, domain, OCR,
subtitle) has already failed, and Lens's reverse-image signal is genuinely
independent of anything already tried. Dry run: apply_links=False, nothing
written to ga_facility_app. Uses 80 of the 250 SerpAPI credits/month.
"""
import sys, os, csv, random, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, lens

random.seed(20260819)

residue_ids = []
with open("docs/phase2_residue_v3.csv", encoding="utf-8-sig") as f:
    residue_ids = [int(r["track_id"]) for r in csv.DictReader(f)]
residue_ids = sorted(set(residue_ids))

needs_linking_ids = [r["track_id"] for r in db.query("""
    SELECT a.track_id FROM ga_app a
    JOIN ga_app_vendor v ON v.track_id = a.track_id AND v.vendor IS NOT NULL
    LEFT JOIN ga_facility_app fa ON fa.track_id = a.track_id
    LEFT JOIN app_crosswalk cw ON cw.track_id = a.track_id
    WHERE a.delisted_at IS NULL AND a.not_a_golf_course_at IS NULL
      AND a.canadian_course_at IS NULL AND fa.track_id IS NULL AND cw.track_id IS NULL
""")]

print(f"residue pool: {len(residue_ids)}, needs-linking pool: {len(needs_linking_ids)}")

residue_sample = random.sample(residue_ids, min(40, len(residue_ids)))
needs_linking_sample = random.sample(needs_linking_ids, min(40, len(needs_linking_ids)))
print(f"sampled {len(residue_sample)} residue, {len(needs_linking_sample)} needs-linking")

source = {tid: "collision_residue" for tid in residue_sample}
source.update({tid: "needs_linking" for tid in needs_linking_sample})
all_ids = residue_sample + needs_linking_sample

with open("docs/lens_pilot_sample.json", "w") as f:
    json.dump({"residue_sample": residue_sample,
              "needs_linking_sample": needs_linking_sample}, f, indent=2)

stats = lens.run_with_matcher(all_ids, apply_links=False)
print(json.dumps(stats.as_dict(), indent=2, default=str))

# ---- export, with source population labeled ---------------------------------
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
    WHERE fa.track_id IN ({','.join(str(t) for t in residue_sample)})
""" if residue_sample else "SELECT 1 WHERE false"):
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
        "CORRECT [y/n]": "", "ACTUAL_FACILITY_ID": "", "NOTES": "",
    })

out.sort(key=lambda r: (r["source_population"], r["track_id"]))
HEADER = ["source_population", "track_id", "app_name", "app_store_url",
          "query_used", "extracted_city_state", "matcher_status",
          "lens_proposed_facility", "current_residue_link",
          "CORRECT [y/n]", "ACTUAL_FACILITY_ID", "NOTES"]
path = "docs/lens_pilot_review.csv"
with open(path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=HEADER)
    w.writeheader()
    w.writerows(out)

print(f"\nwrote {path}: {len(out)} rows")
