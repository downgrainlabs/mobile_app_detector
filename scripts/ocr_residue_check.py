"""OCR the first screenshot for ClubCaddie/Tenfore apps sitting in the
Collision Residue group (an existing but uncertain facility link), and for
the Tenfore needs-linking apps not yet covered. For residue apps, the OCR
evidence is compared against the CURRENTLY LINKED facility -- confirm or
flag a mismatch, same shape as the earlier app-title state-hint rescue.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, screenshot_ocr as ocr, domainmatch
import requests

key = ocr.api_key()

residue_ids = set()
import csv
with open("docs/phase2_residue_v3.csv", encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        if row["vendor"] in ("clubcaddie", "tenfore"):
            residue_ids.add(int(row["track_id"]))
print(f"{len(residue_ids)} clubcaddie/tenfore apps in residue")

tenfore_needs_linking = [r["track_id"] for r in db.query("""
    SELECT a.track_id FROM ga_app a
    JOIN ga_app_vendor v ON v.track_id = a.track_id AND v.vendor = 'tenfore'
    LEFT JOIN ga_facility_app fa ON fa.track_id = a.track_id
    WHERE a.delisted_at IS NULL AND fa.track_id IS NULL
""")]
print(f"{len(tenfore_needs_linking)} tenfore needs-linking apps")

all_ids = sorted(residue_ids | set(tenfore_needs_linking))
apps = {}
for i in range(0, len(all_ids), 400):
    chunk = ",".join(str(t) for t in all_ids[i:i + 400])
    for r in db.query(f"SELECT track_id, track_name FROM ga_app WHERE track_id IN ({chunk})"):
        apps[r["track_id"]] = r["track_name"]

live_links = {}
for r in db.query(f"""
    SELECT track_id, facility_id FROM ga_facility_app
    WHERE track_id IN ({','.join(str(t) for t in all_ids)})
"""):
    live_links.setdefault(r["track_id"], []).append(r["facility_id"])

results = []
for i, tid in enumerate(all_ids, 1):
    row = {"track_id": tid, "app_name": apps.get(tid, ""),
          "live_facility_ids": live_links.get(tid, [])}
    try:
        url = ocr.first_screenshot_url(tid)
        if not url:
            row["status"] = "no_screenshot"
            results.append(row)
            continue
        png = requests.get(url, timeout=30).content
        text = ocr.ocr_text(png, key)
        row["ocr_text_head"] = text[:150].replace("\n", " | ")
        extracted = ocr.extract_address(text)
        sc = ocr.extract_state_country(text)
        cands = ocr.match_facility(extracted)
        row["extracted"] = extracted
        row["ocr_state"] = sc["state"]
        row["ocr_country"] = sc["country"]
        row["street_zip_candidates"] = [{"facility_id": c["facility_id"], "name": c["facility_name"],
                                        "city": c["city"], "state": c["state_code"]} for c in cands]
        row["status"] = "ok"
    except Exception as e:
        row["status"] = "error"
        row["error"] = str(e)[:200]
    results.append(row)
    if i % 10 == 0:
        print(f"  {i}/{len(all_ids)}")
        with open("docs/ocr_residue_check.json", "w") as f:
            json.dump(results, f, indent=2)

with open("docs/ocr_residue_check.json", "w") as f:
    json.dump(results, f, indent=2)
print("wrote docs/ocr_residue_check.json")
