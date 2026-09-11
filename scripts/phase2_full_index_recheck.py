"""Re-check the Phase 2 residue properly: search each app's domain against the
FULL facility.website_url index (~13,922 rows via domainmatch.facility_hosts()),
not the 2-4 candidates match-service happened to suggest by name. That narrow
scoping was the actual bug -- Derek found three exact domain matches (Lakes CC,
Raintree, Inverness) sitting in our own data that this script's predecessor never
even looked at, because the facility they belonged to was never a name-candidate.

Also folds in description-text country scanning (reused from lens.py) as a second
non-US signal alongside TLD checking -- several of Derek's confirmed non-US clubs
(Highlands, Cottonwood, Pinebrook) use plain .com domains with no TLD tell at all,
and are only catchable from what the app's own description says.

Covers two populations:
  1. The 18 remaining duplicate-link track_ids (join.py re-runs left two
     match_service links per app; Derek's sample resolved a few of these already).
  2. The remaining single-link residue from the original 413-row export.
"""
import re
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, domainmatch

_NON_US_COUNTRY_RE = re.compile(
    r"(?i)\b(canada|ontario|british columbia|alberta|quebec|nova scotia|"
    r"saskatchewan|manitoba|united kingdom|england|scotland|wales|ireland|"
    r"australia|new zealand|costa rica)\b")
_NON_US_TLDS = (".ca", ".co.uk", ".uk", ".com.au", ".co.nz", ".ie", ".cr")

skip = domainmatch.vendor_hosts()
print("building full facility domain index...", flush=True)
full_index = domainmatch.facility_hosts()
print(f"  indexed {len(full_index)} distinct hosts across the facility table", flush=True)


def app_hosts_and_text(a):
    hosts = set()
    for field in ("seller_url", "support_url", "privacy_policy_url", "developer_website"):
        h = domainmatch.host(a.get(field))
        if h and not any(h == s or h.endswith("." + s) for s in skip):
            hosts.add(h)
            hosts.add(domainmatch.registrable(h))
    text = f"{a.get('track_name') or ''} {a.get('description') or ''}"
    return hosts, text


def classify(track_id, live_facility_id, a):
    hosts, text = app_hosts_and_text(a)

    non_us = any(h.endswith(_NON_US_TLDS) for h in hosts) or bool(_NON_US_COUNTRY_RE.search(text))
    non_us_evidence = ([h for h in hosts if h.endswith(_NON_US_TLDS)]
                       or _NON_US_COUNTRY_RE.findall(text))

    full_matches = set()
    for h in hosts:
        for f in full_index.get(h, []):
            full_matches.add(f["facility_id"])

    return {
        "track_id": track_id, "app_hosts": sorted(hosts), "non_us": non_us,
        "non_us_evidence": non_us_evidence, "full_index_matches": sorted(full_matches),
        "live_facility_id": live_facility_id,
    }


# ---------------------------------------------------------------- population 1
dup_rows = db.query("""
    SELECT track_id, array_agg(facility_id ORDER BY created_at) AS facility_ids
    FROM ga_facility_app WHERE match_method = 'match_service'
    GROUP BY track_id HAVING count(*) > 1
""")
print(f"\n{len(dup_rows)} track_ids still have duplicate match_service links", flush=True)

apps = {r["track_id"]: r for r in db.query(
    f"SELECT track_id, track_name, description, seller_url, support_url, "
    f"privacy_policy_url, developer_website FROM ga_app WHERE track_id IN "
    f"({','.join(str(r['track_id']) for r in dup_rows)})"
)} if dup_rows else {}

dup_results = []
for r in dup_rows:
    a = apps.get(r["track_id"], {})
    c = classify(r["track_id"], r["facility_ids"][0], a)
    c["all_live_facility_ids"] = r["facility_ids"]
    dup_results.append(c)

# ---------------------------------------------------------------- population 2
crosscheck = json.load(open("docs/phase2_domain_crosscheck.json", encoding="utf-8"))
resolved_ids = {1177073316, 1305628325, 1466636965, 1259202474, 1118436143,
               1348848507, 1457309093, 1506995197, 1542012561, 1619087798,
               1622403131, 6738953628, 6754680278, 6760475467}
residue = [r for r in crosscheck["unresolved"] if r["track_id"] not in resolved_ids]
residue_ids = [r["track_id"] for r in residue if r["track_id"] not in apps]
print(f"{len(residue)} rows remain in the single-link residue (after Derek's fixes)", flush=True)

if residue_ids:
    for i in range(0, len(residue_ids), 400):
        chunk = residue_ids[i:i + 400]
        for row in db.query(
            f"SELECT track_id, track_name, description, seller_url, support_url, "
            f"privacy_policy_url, developer_website FROM ga_app WHERE track_id IN "
            f"({','.join(str(t) for t in chunk)})"
        ):
            apps[row["track_id"]] = row

residue_results = [classify(r["track_id"], r["live_facility_id"], apps.get(r["track_id"], {}))
                   for r in residue]

# ---------------------------------------------------------------- report
def summarize(name, results):
    non_us = [r for r in results if r["non_us"]]
    full_hit = [r for r in results if r["full_index_matches"]
               and not r["non_us"]]
    neither = [r for r in results if not r["non_us"] and not r["full_index_matches"]]
    print(f"\n{name}: {len(results)} total")
    print(f"  non-US (TLD or description)      : {len(non_us)}")
    print(f"  full-index domain match found     : {len(full_hit)}")
    print(f"  still nothing                     : {len(neither)}")
    return non_us, full_hit, neither

dup_nonus, dup_hit, dup_neither = summarize("duplicate-link population", dup_results)
res_nonus, res_hit, res_neither = summarize("single-link residue", residue_results)

with open("docs/phase2_full_index_recheck.json", "w") as f:
    json.dump({
        "duplicates": {"non_us": dup_nonus, "full_index_hit": dup_hit, "neither": dup_neither},
        "residue": {"non_us": res_nonus, "full_index_hit": res_hit, "neither": res_neither},
    }, f, indent=2, default=str)
print("\nwrote docs/phase2_full_index_recheck.json")
