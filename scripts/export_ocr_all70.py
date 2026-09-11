"""Every one of the 70 ClubCaddie apps this OCR pilot touched, not just the
ones that resolved -- so failures/ambiguous cases are visible too, not just
successes.
"""
import sys, os, csv, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db

street_pass = json.load(open("docs/ocr_clubcaddie_pilot2.json", encoding="utf-8"))
state_extract = json.load(open("docs/ocr_state_extract.json", encoding="utf-8"))
state_links = {l["track_id"]: l for l in json.load(
    open("docs/ocr_state_match_results.json", encoding="utf-8"))["links"]}

street_by_id = {r["track_id"]: r for r in street_pass}
state_by_id = {r["track_id"]: r for r in state_extract}
all_ids = sorted(set(street_by_id) | set(state_by_id))
print(f"{len(all_ids)} total ClubCaddie apps in the OCR pilot")

fac_ids = set()
for r in street_by_id.values():
    for c in r.get("candidates", []):
        fac_ids.add(c["facility_id"])
for l in state_links.values():
    fac_ids.add(l["facility_id"])
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
for tid in all_ids:
    s = street_by_id.get(tid, {})
    st = state_by_id.get(tid, {})
    n = state_links.get(tid)
    street_status = s.get("status", "-")
    street_cands = s.get("candidates", [])
    rows.append({
        "track_id": tid,
        "app_name": s.get("app_name") or st.get("app_name", ""),
        "app_store_url": f"https://apps.apple.com/us/app/id{tid}",
        "street_zip_status": street_status,
        "street_zip_extracted": json.dumps(s.get("extracted", {})),
        "street_zip_candidates": "; ".join(label(c["facility_id"]) for c in street_cands),
        "ocr_state": st.get("state", ""),
        "ocr_country": st.get("country", ""),
        "state_name_match": label(n["facility_id"]) if n else "",
        "CORRECT [y/n]": "", "CORRECT_FACILITY_ID": "", "NOTES": "",
    })

STATUS_ORDER = {"unique": 0, "ambiguous": 1, "no_match": 2, "no_screenshot": 3, "error": 4}
rows.sort(key=lambda r: (STATUS_ORDER.get(r["street_zip_status"], 9), r["track_id"]))

HEADER = ["track_id", "app_name", "app_store_url", "street_zip_status",
          "street_zip_extracted", "street_zip_candidates", "ocr_state", "ocr_country",
          "state_name_match", "CORRECT [y/n]", "CORRECT_FACILITY_ID", "NOTES"]
path = "docs/ocr_all70.csv"
with open(path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=HEADER)
    w.writeheader()
    w.writerows(rows)

from collections import Counter
print(Counter(r["street_zip_status"] for r in rows))
print(f"wrote {path}: {len(rows)} rows")
