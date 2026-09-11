"""Re-run OCR matching (with the house-number/zip-collision/prefix fixes) on
just the ambiguous + no_match apps from the first pilot pass -- the 16 unique
matches don't need re-fetching."""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import screenshot_ocr as ocr
import requests

key = ocr.api_key()
prior = json.load(open("docs/ocr_clubcaddie_pilot.json", encoding="utf-8"))
redo = [r for r in prior if r["status"] in ("ambiguous", "no_match")]
print(f"re-running {len(redo)} apps")

kept = [r for r in prior if r["status"] == "unique"]
results = list(kept)
for i, prev in enumerate(redo, 1):
    tid = prev["track_id"]
    row = {"track_id": tid, "app_name": prev["app_name"]}
    try:
        url = ocr.first_screenshot_url(tid)
        if not url:
            row["status"] = "no_screenshot"
            results.append(row)
            continue
        png = requests.get(url, timeout=30).content
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
        print(f"  {i}/{len(redo)}")

with open("docs/ocr_clubcaddie_pilot2.json", "w") as f:
    json.dump(results, f, indent=2)

from collections import Counter
print(Counter(r["status"] for r in results))
print("wrote docs/ocr_clubcaddie_pilot2.json")
