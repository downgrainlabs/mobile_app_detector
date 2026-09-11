"""Dry run: OCR the first screenshot of every unresolved ClubCaddie app, extract
a street address + zip, and match against the facility table. Reports precision
before anything gets written -- established rule this session: never
auto-apply a new signal without checking it first (the 42-wrong-link Lens
incident is why).
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, screenshot_ocr as ocr

key = ocr.api_key()

apps = db.query("""
    SELECT a.track_id, a.track_name FROM ga_app a
    JOIN ga_app_vendor v ON v.track_id = a.track_id AND v.vendor = 'clubcaddie'
    LEFT JOIN ga_facility_app fa ON fa.track_id = a.track_id
    WHERE a.delisted_at IS NULL AND fa.track_id IS NULL
    ORDER BY a.track_id
""")
print(f"{len(apps)} unresolved ClubCaddie apps")

results = []
for i, a in enumerate(apps, 1):
    row = {"track_id": a["track_id"], "app_name": a["track_name"]}
    try:
        url = ocr.first_screenshot_url(a["track_id"])
        if not url:
            row["status"] = "no_screenshot"
            results.append(row)
            continue
        png = __import__("requests").get(url, timeout=30).content
        text = ocr.ocr_text(png, key)
        extracted = ocr.extract_address(text)
        cands = ocr.match_facility(extracted)
        row["extracted"] = extracted
        row["candidates"] = [{"facility_id": c["facility_id"], "name": c["facility_name"],
                              "city": c["city"], "state": c["state_code"],
                              "address": c["facility_street_address"]} for c in cands]
        row["status"] = ("unique" if len(cands) == 1 else
                         "ambiguous" if len(cands) > 1 else "no_match")
    except Exception as e:
        row["status"] = "error"
        row["error"] = str(e)[:200]
    results.append(row)
    if i % 10 == 0:
        print(f"  {i}/{len(apps)}")
        with open("docs/ocr_clubcaddie_pilot.json", "w") as f:
            json.dump(results, f, indent=2)

with open("docs/ocr_clubcaddie_pilot.json", "w") as f:
    json.dump(results, f, indent=2)

from collections import Counter
print(Counter(r["status"] for r in results))
print("wrote docs/ocr_clubcaddie_pilot.json")
