"""Combine both OCR-matching passes (street/zip pilot2, state+name match-service)
into one review CSV. Where both passes independently found a candidate for the
same app, flag AGREE/DISAGREE -- that's the strongest signal in the file.
"""
import sys, os, csv, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db

street_pass = json.load(open("docs/ocr_clubcaddie_pilot2.json", encoding="utf-8"))
state_pass = json.load(open("docs/ocr_state_match_results.json", encoding="utf-8"))

street_hits = {r["track_id"]: r["candidates"][0] for r in street_pass
              if r["status"] == "unique"}
state_hits = {l["track_id"]: l for l in state_pass["links"]}

all_ids = sorted(set(street_hits) | set(state_hits))
print(f"{len(all_ids)} apps with a candidate from at least one pass")

apps = {}
for i in range(0, len(all_ids), 400):
    chunk = ",".join(str(t) for t in all_ids[i:i + 400])
    for r in db.query(f"SELECT track_id, track_name FROM ga_app WHERE track_id IN ({chunk})"):
        apps[r["track_id"]] = r["track_name"]

fac_ids = {h["facility_id"] for h in street_hits.values()} | {l["facility_id"] for l in state_hits.values()}
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
    s = street_hits.get(tid)
    n = state_hits.get(tid)
    s_fid = s["facility_id"] if s else None
    n_fid = n["facility_id"] if n else None
    if s_fid and n_fid:
        agreement = "AGREE" if s_fid == n_fid else "DISAGREE"
    else:
        agreement = "street_only" if s_fid else "state_only"
    rows.append({
        "track_id": tid,
        "app_name": apps.get(tid, ""),
        "app_store_url": f"https://apps.apple.com/us/app/id{tid}",
        "agreement": agreement,
        "street_zip_match": label(s_fid) if s_fid else "",
        "state_name_match": label(n_fid) if n_fid else "",
        "CORRECT [y/n]": "", "CORRECT_FACILITY_ID": "", "NOTES": "",
    })

rows.sort(key=lambda r: ({"DISAGREE": 0, "AGREE": 1, "street_only": 2,
                          "state_only": 3}[r["agreement"]], r["track_id"]))

HEADER = ["track_id", "app_name", "app_store_url", "agreement",
          "street_zip_match", "state_name_match",
          "CORRECT [y/n]", "CORRECT_FACILITY_ID", "NOTES"]
path = "docs/ocr_review.csv"
with open(path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=HEADER)
    w.writeheader()
    w.writerows(rows)

from collections import Counter
print(Counter(r["agreement"] for r in rows))
print(f"wrote {path}: {len(rows)} rows")
