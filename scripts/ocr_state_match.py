"""OCR the first screenshot of unresolved ClubCaddie apps for state + country,
then resolve (clean app name, OCR state) through match-service -- the same
resolution path join.py already uses for every other app. Simpler than trying
to parse/match full street addresses: state alone narrows match-service's
name search enough to usually land the right facility, and a non-US country
hit gets the same country-lock treatment as the Lens channel (skip, don't
force a US match).

Dry run by default -- prints what WOULD be written. Pass apply=True to write.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, screenshot_ocr as ocr, join, config
import requests

APPLY = "--apply" in sys.argv

key = ocr.api_key()

apps = db.query("""
    SELECT a.track_id, a.track_name FROM ga_app a
    JOIN ga_app_vendor v ON v.track_id = a.track_id AND v.vendor = 'clubcaddie'
    LEFT JOIN ga_facility_app fa ON fa.track_id = a.track_id
    WHERE a.delisted_at IS NULL AND fa.track_id IS NULL
    ORDER BY a.track_id
""")
print(f"{len(apps)} unresolved ClubCaddie apps")

rows = []
for i, a in enumerate(apps, 1):
    row = {"track_id": a["track_id"], "app_name": a["track_name"]}
    try:
        url = ocr.first_screenshot_url(a["track_id"])
        if not url:
            row["status"] = "no_screenshot"
            rows.append(row)
            continue
        png = requests.get(url, timeout=30).content
        text = ocr.ocr_text(png, key)
        sc = ocr.extract_state_country(text)
        row["state"] = sc["state"]
        row["country"] = sc["country"]
        row["status"] = ("non_us" if sc["country"] and sc["country"] != "US"
                         else "state_found" if sc["state"] else "no_state")
    except Exception as e:
        row["status"] = "error"
        row["error"] = str(e)[:200]
    rows.append(row)
    if i % 10 == 0:
        print(f"  {i}/{len(apps)}")

from collections import Counter
print(Counter(r["status"] for r in rows))
with open("docs/ocr_state_extract.json", "w") as f:
    json.dump(rows, f, indent=2)

# ---- submit the state_found ones to match-service --------------------------
to_submit = [r for r in rows if r["status"] == "state_found"]
print(f"\nsubmitting {len(to_submit)} to match-service")

by_id = {a["track_id"]: a for a in apps}
items, index = [], []
for r in to_submit:
    name = join.clean_name(by_id[r["track_id"]]["track_name"])
    if not name or len(name) < 3:
        continue
    items.append({"facility_name": name, "state": r["state"]})
    index.append(r["track_id"])

in_scope = {row["facility_id"] for row in db.query(
    f"SELECT facility_id FROM facility WHERE {config.FACILITY_WHERE}")}

links, unmatched = [], []
for i in range(0, len(items), config.MATCH_BATCH_SIZE):
    chunk = items[i:i + config.MATCH_BATCH_SIZE]
    ids = index[i:i + config.MATCH_BATCH_SIZE]
    job = join._submit(chunk, config.MATCH_CONFIDENCE_THRESHOLD)
    results = join._poll(job, total=len(chunk))
    for track_id, req, res in zip(ids, chunk, results):
        status = res.get("matchStatus") or "unknown"
        fid = res.get("facility_id")
        if fid and int(fid) not in in_scope:
            fid = None
        if fid:
            links.append({"facility_id": int(fid), "track_id": track_id,
                          "match_confidence": res.get("confidence"),
                          "match_method": "ocr_state_match", "match_status": status,
                          "submitted": req})
        else:
            unmatched.append({"track_id": track_id, "submitted": req, "status": status})

print(f"\nmatched: {len(links)}  unmatched: {len(unmatched)}")
for l in links:
    print(f"  {l['track_id']:12d} {l['submitted']!r:55s} -> #{l['facility_id']} "
         f"({l['match_confidence']}) {l['match_status']}")

with open("docs/ocr_state_match_results.json", "w") as f:
    json.dump({"links": links, "unmatched": unmatched}, f, indent=2, default=str)

if APPLY and links:
    write_rows = [{k: v for k, v in l.items() if k != "submitted"} for l in links]
    db.upsert("ga_facility_app", write_rows, on_conflict="facility_id,track_id")
    print(f"\napplied {len(write_rows)} links")
else:
    print("\nDRY RUN -- nothing written. Re-run with --apply to write.")
