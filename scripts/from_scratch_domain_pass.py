"""Supplementary to from_scratch_dry_run.py: domain matching is a real,
general-purpose pipeline channel (domainmatch.py), not a one-off script --
skipping it undercounted Looks Good. Runs it ONLY against the needs_lens
pool from the match-service pass, ignoring the real ga_facility_app table
entirely (this is a from-scratch simulation, not touching production data).
"""
import sys, os, csv, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, domainmatch

needs_lens = json.load(open("docs/from_scratch_needs_lens.json", encoding="utf-8"))
needs_lens_ids = {r["track_id"] for r in needs_lens}
print(f"{len(needs_lens_ids)} apps in the needs_lens pool to domain-check")

skip = domainmatch.vendor_hosts()
facs = domainmatch.facility_hosts()

rows = db.query(f"""
    SELECT track_id, seller_url, support_url, privacy_policy_url, developer_website
    FROM ga_app WHERE track_id IN ({','.join(str(t) for t in needs_lens_ids)})
""")

resolved, ambiguous = [], []
for r in rows:
    hits = []
    for field in ("seller_url", "support_url", "privacy_policy_url", "developer_website"):
        h = domainmatch.host(r.get(field))
        if not h or any(h == s or h.endswith("." + s) for s in skip):
            continue
        hits.extend(facs.get(h, []))
        reg = domainmatch.registrable(h)
        if reg and reg != h:
            hits.extend(facs.get(reg, []))
    if not hits:
        continue
    unique = {f["facility_id"]: f for f in hits}
    if len(unique) == 1:
        fac = next(iter(unique.values()))
        resolved.append({"track_id": r["track_id"], "facility_id": fac["facility_id"],
                         "match_confidence": 0.95, "match_status": "Match - Website Domain"})
    else:
        ambiguous.append(r["track_id"])

print(f"{len(resolved)} resolved via domain, {len(ambiguous)} ambiguous (multiple facilities same domain)")

resolved_ids = {r["track_id"] for r in resolved}
new_needs_lens = [r for r in needs_lens if r["track_id"] not in resolved_ids]

with open("docs/from_scratch_domain_resolved.json", "w") as f:
    json.dump(resolved, f, indent=2, default=str)

# ---- merge into the looks_good / needs_lens CSVs -----------------------------
looks_good = json.load(open("docs/from_scratch_looks_good.json", encoding="utf-8"))
looks_good_all = looks_good + [{**r, "submitted_name": "", "submitted_city": "",
                                "submitted_state": ""} for r in resolved]

fac_ids = {r["facility_id"] for r in looks_good_all}
facs_info = {}
ids = list(fac_ids)
for i in range(0, len(ids), 400):
    chunk = ",".join(str(x) for x in ids[i:i + 400])
    for f in db.query(f"SELECT facility_id, facility_name, city, state_code FROM facility "
                      f"WHERE facility_id IN ({chunk})"):
        facs_info[f["facility_id"]] = f

app_ids = [r["track_id"] for r in looks_good_all] + [r["track_id"] for r in new_needs_lens]
apps = {}
for i in range(0, len(app_ids), 400):
    chunk = ",".join(str(t) for t in app_ids[i:i + 400])
    for r in db.query(f"SELECT track_id, track_name FROM ga_app WHERE track_id IN ({chunk})"):
        apps[r["track_id"]] = r["track_name"]

with open("docs/from_scratch_looks_good_v2.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["track_id", "app_name", "app_store_url", "facility_id", "facility_name",
               "city", "state", "match_confidence", "match_status"])
    for r in looks_good_all:
        fac = facs_info.get(r["facility_id"], {})
        w.writerow([r["track_id"], apps.get(r["track_id"], ""),
                   f"https://apps.apple.com/us/app/id{r['track_id']}", r["facility_id"],
                   fac.get("facility_name", ""), fac.get("city", ""), fac.get("state_code", ""),
                   r["match_confidence"], r["match_status"]])

with open("docs/from_scratch_needs_lens_v2.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["track_id", "app_name", "app_store_url", "reason", "match_status",
               "submitted_name", "submitted_city", "submitted_state", "candidate_count"])
    for r in new_needs_lens:
        w.writerow([r["track_id"], apps.get(r["track_id"], ""),
                   f"https://apps.apple.com/us/app/id{r['track_id']}", r["reason"],
                   r["match_status"], r["submitted_name"], r["submitted_city"],
                   r["submitted_state"], r["candidate_count"]])

print(f"\nfinal: {len(looks_good_all)} looks_good, {len(new_needs_lens)} needs_lens")
print("wrote docs/from_scratch_looks_good_v2.csv and docs/from_scratch_needs_lens_v2.csv")
