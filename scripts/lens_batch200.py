"""Run the two-stage Lens+matcher pipeline against 200 apps that survived
every earlier layer (match_service, domain, subtitle, OCR, vendor/non-US/
golf-signal classify()) and genuinely need Lens. Dry run -- apply_links=False
-- Derek is reviewing manually before anything gets written.
"""
import sys, os, csv, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, lens

batch = json.load(open("docs/lens_batch200_ids.json", encoding="utf-8"))
print(f"running Lens+matcher on {len(batch)} apps")

stats = lens.run_with_matcher(batch, apply_links=False)
print(json.dumps(stats.as_dict(), indent=2, default=str))

rows = db.query(f"""
    SELECT l.track_id, a.track_name, l.query_used, l.extracted_city,
           l.extracted_state, l.matcher_status, l.matcher_facility_id,
           f.facility_name, f.city, f.state_code
    FROM ga_lens_detection l
    JOIN ga_app a ON a.track_id = l.track_id
    LEFT JOIN facility f ON f.facility_id = l.matcher_facility_id
    WHERE l.track_id IN ({','.join(str(t) for t in batch)})
""")

out = []
for r in rows:
    cs = (f"{r['extracted_city'] or ''}, {r['extracted_state'] or ''}"
         if (r["extracted_city"] or r["extracted_state"]) else "")
    prop = (f"#{r['matcher_facility_id']} {r['facility_name']} | "
           f"{r['city']}, {r['state_code']}") if r["matcher_facility_id"] else ""
    out.append({
        "track_id": r["track_id"], "app_name": r["track_name"],
        "app_store_url": f"https://apps.apple.com/us/app/id{r['track_id']}",
        "query_used": r["query_used"] or "", "extracted_city_state": cs,
        "matcher_status": r["matcher_status"] or "(no Lens result)",
        "lens_proposed_facility": prop,
        "CORRECT [y/n]": "", "ACTUAL_FACILITY_ID": "", "NOTES": "",
    })

out.sort(key=lambda r: r["track_id"])
HEADER = ["track_id", "app_name", "app_store_url", "query_used", "extracted_city_state",
         "matcher_status", "lens_proposed_facility", "CORRECT [y/n]", "ACTUAL_FACILITY_ID", "NOTES"]
path = "docs/lens_batch200_review.csv"
with open(path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=HEADER)
    w.writeheader()
    w.writerows(out)
print(f"wrote {path}: {len(out)} rows")
