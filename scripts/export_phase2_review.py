"""Review CSV for the Phase 2 collision audit.

Disagreements first (28 -- the app's own resubmitted top choice differs from what's
live; caveat: match-service showed real instability on near-duplicate queries in the
same batch -- the two Shoreline apps got their answers SWAPPED between runs, so a
disagreement is a reason to look, not proof the live link is wrong). Then collisions
(523 -- live link agrees with a fresh resubmission, but a second real, in-scope Golf
Course candidate also came back; most will probably confirm fine on inspection, but
this is exactly the shape of the Ridgewood/Arrowhead errors found today).
"""
import sys, os, csv, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db

d = json.load(open("docs/phase2_collision_audit.json", encoding="utf-8"))

facility_ids = set()
for r in d["disagreements"] + d["collisions"]:
    facility_ids.add(r["live_facility_id"])
    if r.get("fresh_facility_id"):
        facility_ids.add(r["fresh_facility_id"])
    for c in r.get("other_candidates", []):
        if c.get("facility_id"):
            facility_ids.add(int(c["facility_id"]))

facs = {}
ids = list(facility_ids)
for i in range(0, len(ids), 400):
    chunk = ",".join(str(x) for x in ids[i:i + 400])
    for f in db.query(f"SELECT facility_id, facility_name, city, state_code "
                      f"FROM facility WHERE facility_id IN ({chunk})"):
        facs[f["facility_id"]] = f


def fac_label(fid):
    f = facs.get(fid)
    return f"{fid} | {f['facility_name']} | {f['city']}, {f['state_code']}" if f else str(fid)


rows = []
for r in d["disagreements"]:
    rows.append({
        "source": "DISAGREEMENT",
        "track_id": r["track_id"], "app_name": r["app_name"],
        "app_store_url": f"https://apps.apple.com/us/app/id{r['track_id']}",
        "live_link": fac_label(r["live_facility_id"]),
        "fresh_top_pick": fac_label(r["fresh_facility_id"]) if r["fresh_facility_id"] else "no match",
        "other_candidates": "",
        "CORRECT_AS_LIVE [y/n]": "", "CORRECTED_FACILITY_ID": "", "NOTES": "",
    })
for r in d["collisions"]:
    others = "; ".join(fac_label(int(c["facility_id"])) for c in r["other_candidates"])
    rows.append({
        "source": "COLLISION",
        "track_id": r["track_id"], "app_name": r["app_name"],
        "app_store_url": f"https://apps.apple.com/us/app/id{r['track_id']}",
        "live_link": fac_label(r["live_facility_id"]),
        "fresh_top_pick": fac_label(r["fresh_facility_id"]),
        "other_candidates": others,
        "CORRECT_AS_LIVE [y/n]": "", "CORRECTED_FACILITY_ID": "", "NOTES": "",
    })

HEADER = ["source", "track_id", "app_name", "app_store_url", "live_link",
          "fresh_top_pick", "other_candidates",
          "CORRECT_AS_LIVE [y/n]", "CORRECTED_FACILITY_ID", "NOTES"]
path = "docs/phase2_review.csv"
with open(path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=HEADER)
    w.writeheader()
    w.writerows(rows)

print(json.dumps({"path": path, "rows": len(rows),
                  "disagreements": len(d["disagreements"]),
                  "collisions": len(d["collisions"])}, indent=2))
