"""Review CSV for the 413 Phase 2 links the domain cross-check couldn't resolve.

Sorted by reason so patterns are visible at a glance, not buried row-by-row:
  1. app_domain_is_non_us (11)                -- live link is likely just wrong
  2. domain_matches_none_of_the_candidates (56) -- our own data may be stale, or the
                                                    real answer isn't among the
                                                    candidates match-service offered
  3. no_url_data (346)                         -- no club-specific domain evidence
                                                    survives vendor/platform filtering
"""
import sys, os, csv, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db

d = json.load(open("docs/phase2_domain_crosscheck.json", encoding="utf-8"))
unresolved = d["unresolved"]

fac_ids = set()
for r in unresolved:
    fac_ids.add(r["live_facility_id"])
    if r.get("fresh_facility_id"):
        fac_ids.add(r["fresh_facility_id"])
    for c in r.get("other_candidates", []):
        if c.get("facility_id"):
            fac_ids.add(int(c["facility_id"]))

facs = {}
ids = list(fac_ids)
for i in range(0, len(ids), 400):
    chunk = ",".join(str(x) for x in ids[i:i + 400])
    for f in db.query(f"SELECT facility_id, facility_name, city, state_code, website_url "
                      f"FROM facility WHERE facility_id IN ({chunk})"):
        facs[f["facility_id"]] = f


def fac_label(fid):
    f = facs.get(fid)
    if not f:
        return str(fid)
    site = f" [{f['website_url']}]" if f.get("website_url") else ""
    return f"{fid} | {f['facility_name']} | {f['city']}, {f['state_code']}{site}"


REASON_ORDER = {"app_domain_is_non_us": 0,
                "domain_matches_none_of_the_candidates": 1,
                "no_url_data": 2}

rows = []
for r in unresolved:
    others = "; ".join(fac_label(int(c["facility_id"])) for c in r.get("other_candidates", []))
    rows.append({
        "reason": r.get("reason", ""),
        "track_id": r["track_id"], "app_name": r["app_name"],
        "app_store_url": f"https://apps.apple.com/us/app/id{r['track_id']}",
        "app_hosts": ", ".join(r.get("app_hosts", [])),
        "live_link": fac_label(r["live_facility_id"]),
        "other_candidates": others,
        "CORRECT_AS_LIVE [y/n]": "", "CORRECTED_FACILITY_ID": "",
        "REMOVE_LIVE_LINK [y/n]": "", "NOTES": "",
    })

rows.sort(key=lambda r: (REASON_ORDER.get(r["reason"], 9), r["track_id"]))

HEADER = ["reason", "track_id", "app_name", "app_store_url", "app_hosts",
          "live_link", "other_candidates",
          "CORRECT_AS_LIVE [y/n]", "CORRECTED_FACILITY_ID",
          "REMOVE_LIVE_LINK [y/n]", "NOTES"]
path = "docs/phase2_residue.csv"
with open(path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=HEADER)
    w.writeheader()
    w.writerows(rows)

from collections import Counter
print(json.dumps({"path": path, "rows": len(rows),
                  "by_reason": dict(Counter(r["reason"] for r in rows))}, indent=2))
