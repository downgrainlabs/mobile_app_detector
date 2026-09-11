"""Import Derek's manually-reviewed app->facility CSV into app_crosswalk.
Comma-delimited facility_id lists become facility_ids arrays -- the only
sanctioned way for one app to map to multiple facilities. Validates every
facility_id actually exists, checks for collisions with existing crosswalk
rows (e.g. 'exclude' entries), and clears any existing non-crosswalk link
for these apps so the crosswalk becomes the sole source of truth for them.
"""
import sys, os, csv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, join

rows = list(csv.DictReader(open("docs/manual_crosswalk.csv", encoding="utf-8-sig")))
print(f"{len(rows)} rows in the CSV")

records = []
for r in rows:
    tid = int(r["track_id"])
    raw = r["facility_id"]
    fids = [int(x.strip()) for x in raw.split(",") if x.strip()]
    records.append({"track_id": tid, "facility_ids": fids})

# ---- validate facility_ids exist ---------------------------------------------
all_fids = {fid for r in records for fid in r["facility_ids"]}
existing_fids = {row["facility_id"] for row in db.query(
    f"SELECT facility_id FROM facility WHERE facility_id IN ({','.join(str(f) for f in all_fids)})"
)}
missing = all_fids - existing_fids
if missing:
    print(f"WARNING: {len(missing)} facility_ids don't exist in facility table: {missing}")
    for r in records:
        bad = [f for f in r["facility_ids"] if f in missing]
        if bad:
            print(f"  track_id {r['track_id']}: bad facility_ids {bad}")

# ---- check collision with existing crosswalk rows -----------------------------
existing_cw = {row["track_id"]: row for row in db.query(
    f"SELECT track_id, link_type, exclude_reason FROM app_crosswalk WHERE track_id IN "
    f"({','.join(str(r['track_id']) for r in records)})"
)}
if existing_cw:
    print(f"\n{len(existing_cw)} of these track_ids already have a crosswalk row "
         f"(will be overwritten by this import):")
    for tid, row in existing_cw.items():
        print(f"  {tid}: was {row['link_type']} ({row.get('exclude_reason')})")

# ---- check existing non-crosswalk links (from match_service/domain/etc) ------
existing_links = db.query(f"""
    SELECT track_id, facility_id, match_method FROM ga_facility_app
    WHERE track_id IN ({','.join(str(r['track_id']) for r in records)})
""")
if existing_links:
    print(f"\n{len(existing_links)} existing (non-crosswalk) links for these apps -- "
         f"will be replaced by the crosswalk's answer:")
    for l in existing_links[:20]:
        print(f"  {l['track_id']} -> #{l['facility_id']} (was {l['match_method']})")

# ---- apply ---------------------------------------------------------------
multi = sum(1 for r in records if len(r["facility_ids"]) > 1)
print(f"\n{len(records)} apps total, {multi} with multiple facility_ids")

crosswalk_rows = [{
    "track_id": r["track_id"], "link_type": "facility",
    "facility_ids": r["facility_ids"], "source": "manual_review",
} for r in records]
db.upsert("app_crosswalk", crosswalk_rows, on_conflict="track_id")
print(f"upserted {len(crosswalk_rows)} app_crosswalk rows")

touched_ids = [r["track_id"] for r in records]
for i in range(0, len(touched_ids), 400):
    chunk = ",".join(str(t) for t in touched_ids[i:i + 400])
    db.exec_sql(f"DELETE FROM ga_facility_app WHERE track_id IN ({chunk}) "
               f"AND match_method != 'manual_crosswalk'")

crosswalked = join.apply_crosswalk()
print(f"\napply_crosswalk() materialized links for {len(crosswalked)} crosswalked track_ids total")

final_links = db.query(f"""
    SELECT count(*) c FROM ga_facility_app
    WHERE track_id IN ({','.join(str(t) for t in touched_ids)}) AND match_method='manual_crosswalk'
""")[0]["c"]
print(f"final: {final_links} ga_facility_app rows now exist for these {len(records)} apps "
     f"(expect {sum(len(r['facility_ids']) for r in records)} if all facility_ids were valid)")
