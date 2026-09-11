"""Re-run Lens+matcher on the 40 apps skipped in the first pilot pass purely
for missing artwork_url -- now that run_with_matcher() backfills the icon
via a free iTunes lookup instead of dropping the app. Merges into the same
review CSV so the full 80-app sample (40 residue + 40 needs-linking) is
represented in one file.
"""
import sys, os, csv, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, lens

skipped = json.load(open("docs/lens_pilot_skipped.json"))
sample = json.load(open("docs/lens_pilot_sample.json"))
source = {tid: "collision_residue" for tid in sample["residue_sample"]}
source.update({tid: "needs_linking" for tid in sample["needs_linking_sample"]})

print(f"re-running {len(skipped)} previously-skipped apps")
stats = lens.run_with_matcher(skipped, apply_links=False)
print(json.dumps(stats.as_dict(), indent=2, default=str))

all_ids = sample["residue_sample"] + sample["needs_linking_sample"]
rows = db.query(f"""
    SELECT l.track_id, a.track_name, l.query_used, l.extracted_city,
           l.extracted_state, l.matcher_status, l.matcher_facility_id,
           f.facility_name, f.city, f.state_code
    FROM ga_lens_detection l
    JOIN ga_app a ON a.track_id = l.track_id
    LEFT JOIN facility f ON f.facility_id = l.matcher_facility_id
    WHERE l.track_id IN ({','.join(str(t) for t in all_ids)})
""")

residue_sample = sample["residue_sample"]
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
path = "docs/lens_pilot_review_v2.csv"
with open(path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=HEADER)
    w.writeheader()
    w.writerows(out)

print(f"\nwrote {path}: {len(out)} rows")
