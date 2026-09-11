"""Export the 'Looks Good' population -- every app with a live facility link
that is NOT in the current collision-residue file (docs/phase2_residue_v2.csv).
"""
import sys, os, csv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db

residue_ids = set()
with open("docs/phase2_residue_v3.csv", encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        residue_ids.add(int(row["track_id"]))
print(f"excluding {len(residue_ids)} residue track_ids")

links = db.query("""
    SELECT fa.track_id, fa.facility_id, fa.match_method, fa.match_confidence, fa.match_status
    FROM ga_facility_app fa
    ORDER BY fa.track_id
""")
links = [l for l in links if l["track_id"] not in residue_ids]
print(f"{len(links)} looks-good links")

app_ids = sorted({l["track_id"] for l in links})
apps = {}
for i in range(0, len(app_ids), 400):
    chunk = ",".join(str(t) for t in app_ids[i:i + 400])
    for r in db.query(f"SELECT track_id, track_name FROM ga_app WHERE track_id IN ({chunk})"):
        apps[r["track_id"]] = r["track_name"]

vendors = {}
for i in range(0, len(app_ids), 400):
    chunk = ",".join(str(t) for t in app_ids[i:i + 400])
    for r in db.query(f"SELECT track_id, vendor FROM ga_app_vendor WHERE track_id IN ({chunk})"):
        if r["vendor"]:
            vendors[r["track_id"]] = r["vendor"]

fac_ids = sorted({l["facility_id"] for l in links})
facs = {}
for i in range(0, len(fac_ids), 400):
    chunk = ",".join(str(f) for f in fac_ids[i:i + 400])
    for f in db.query(f"SELECT facility_id, facility_name, city, state_code, website_url "
                      f"FROM facility WHERE facility_id IN ({chunk})"):
        facs[f["facility_id"]] = f

no_vendor = [l for l in links if l["track_id"] not in vendors]
print(f"dropping {len(no_vendor)} links with no known vendor signature")
for l in no_vendor[:100]:
    print("   ", l["track_id"], apps.get(l["track_id"], ""))
links = [l for l in links if l["track_id"] in vendors]

rows = []
for l in links:
    tid = l["track_id"]
    f = facs.get(l["facility_id"], {})
    rows.append({
        "track_id": tid,
        "app_name": apps.get(tid, ""),
        "vendor": vendors.get(tid, ""),
        "app_store_url": f"https://apps.apple.com/us/app/id{tid}",
        "facility_id": l["facility_id"],
        "facility_name": f.get("facility_name", ""),
        "city": f.get("city", ""),
        "state": f.get("state_code", ""),
        "facility_website": f.get("website_url", ""),
        "match_method": l["match_method"],
        "match_confidence": l["match_confidence"],
        "match_status": l["match_status"],
        "WRONG [y]": "", "NOTES": "",
    })

rows.sort(key=lambda r: (r["vendor"] or "zzz", r["app_name"]))

HEADER = ["track_id", "app_name", "vendor", "app_store_url", "facility_id",
          "facility_name", "city", "state", "facility_website",
          "match_method", "match_confidence", "match_status",
          "WRONG [y]", "NOTES"]
path = "docs/phase2_looks_good_v2.csv"
with open(path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=HEADER)
    w.writeheader()
    w.writerows(rows)

print(f"wrote {path}: {len(rows)} rows")
