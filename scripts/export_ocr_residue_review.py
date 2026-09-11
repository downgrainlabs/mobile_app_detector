"""Full detail review CSV for the OCR residue-check pass: ClubCaddie/Tenfore
apps in the collision residue (existing but uncertain link) plus Tenfore's
needs-linking apps. Shows the raw OCR text head and both sides' street
addresses so a MISMATCH tag can actually be judged, not just trusted.
"""
import sys, os, csv, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db

data = json.load(open("docs/ocr_residue_check.json", encoding="utf-8"))

fac_ids = set()
for r in data:
    fac_ids.update(r.get("live_facility_ids", []))
    for c in r.get("street_zip_candidates", []):
        fac_ids.add(c["facility_id"])
facs = {}
ids = list(fac_ids)
for i in range(0, len(ids), 400):
    chunk = ",".join(str(f) for f in ids[i:i + 400])
    for f in db.query(f"SELECT facility_id, facility_name, city, state_code, "
                      f"facility_street_address FROM facility WHERE facility_id IN ({chunk})"):
        facs[f["facility_id"]] = f


def label(fid):
    f = facs.get(fid, {})
    return f"#{fid} {f.get('facility_name','')} | {f.get('city','')}, {f.get('state_code','')} | {f.get('facility_street_address','')}"


rows = []
for r in data:
    live = r.get("live_facility_ids", [])
    cands = r.get("street_zip_candidates", [])
    cand_ids = {c["facility_id"] for c in cands}
    if not live:
        tag = "NEEDS_LINKING"
    elif cand_ids and cand_ids == set(live):
        tag = "CONFIRM"
    elif cand_ids and not (cand_ids & set(live)):
        tag = "MISMATCH"
    elif cand_ids:
        tag = "PARTIAL"
    else:
        tag = "no_evidence"

    rows.append({
        "tag": tag,
        "track_id": r["track_id"],
        "app_name": r["app_name"],
        "app_store_url": f"https://apps.apple.com/us/app/id{r['track_id']}",
        "ocr_text_head": r.get("ocr_text_head", ""),
        "ocr_extracted_street": "; ".join(r.get("extracted", {}).get("street_candidates", [])),
        "ocr_state": r.get("ocr_state", ""),
        "live_link": "; ".join(label(f) for f in live),
        "ocr_candidates": "; ".join(label(c["facility_id"]) for c in cands),
        "CORRECT_FACILITY_ID": "", "NOTES": "",
    })

TAG_ORDER = {"MISMATCH": 0, "PARTIAL": 1, "NEEDS_LINKING": 2, "CONFIRM": 3, "no_evidence": 4}
rows.sort(key=lambda r: (TAG_ORDER.get(r["tag"], 9), r["track_id"]))

HEADER = ["tag", "track_id", "app_name", "app_store_url", "ocr_text_head",
          "ocr_extracted_street", "ocr_state", "live_link", "ocr_candidates",
          "CORRECT_FACILITY_ID", "NOTES"]
path = "docs/ocr_residue_review.csv"
with open(path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=HEADER)
    w.writeheader()
    w.writerows(rows)

from collections import Counter
print(Counter(r["tag"] for r in rows))
print(f"wrote {path}: {len(rows)} rows")
