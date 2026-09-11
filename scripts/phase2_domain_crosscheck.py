"""Resolve as much of the Phase 2 collision/disagreement audit as possible using
domain evidence, before asking for human review.

For each flagged link, pull the app's own seller_url / support_url /
privacy_policy_url / developer_website and check which facility (if any) they
actually match via facility.website_url -- the same exact/registrable-domain logic
domainmatch.py already used to validate 692 links earlier in this project.

Three outcomes:
  CONFIRMED_BY_DOMAIN    -- a host matches the LIVE facility. No action; drop from
                            the review queue.
  CORRECTED_BY_DOMAIN    -- a host matches a DIFFERENT real candidate than what's
                            live. This is the same strength of evidence the Lens
                            pipeline auto-applied without per-item human review
                            (692 agreements, the direct domain links). Apply the
                            fix, log every change plainly so it can be audited or
                            reverted.
  NO_DOMAIN_SIGNAL       -- no host resolves anything. Genuinely needs a human;
                            stays in the review queue.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, domainmatch

_NON_US_TLDS = (".ca", ".co.uk", ".uk", ".com.au", ".co.nz", ".ie")

d = json.load(open("docs/phase2_collision_audit.json", encoding="utf-8"))
flagged = d["disagreements"] + d["collisions"]
print(f"cross-checking {len(flagged)} flagged links against domain evidence", flush=True)

track_ids = [r["track_id"] for r in flagged]
apps = {r["track_id"]: r for r in db.query(
    f"SELECT track_id, seller_url, support_url, privacy_policy_url, developer_website "
    f"FROM ga_app WHERE track_id IN ({','.join(str(t) for t in track_ids)})"
)}

# facility_id -> set of hosts (exact + registrable) that facility's website_url maps to
fac_ids = set()
for r in flagged:
    fac_ids.add(r["live_facility_id"])
    if r.get("fresh_facility_id"):
        fac_ids.add(r["fresh_facility_id"])
    for c in r.get("other_candidates", []):
        if c.get("facility_id"):
            fac_ids.add(int(c["facility_id"]))

fac_hosts = {}
ids = list(fac_ids)
for i in range(0, len(ids), 400):
    chunk = ",".join(str(x) for x in ids[i:i + 400])
    for f in db.query(f"SELECT facility_id, website_url FROM facility "
                      f"WHERE facility_id IN ({chunk})"):
        h = domainmatch.host(f["website_url"])
        if h:
            fac_hosts[f["facility_id"]] = {h, domainmatch.registrable(h)}

skip = domainmatch.vendor_hosts()

confirmed, corrected, unresolved = [], [], []
for r in flagged:
    a = apps.get(r["track_id"], {})
    app_hosts = set()
    for field in ("seller_url", "support_url", "privacy_policy_url", "developer_website"):
        h = domainmatch.host(a.get(field))
        if h and not any(h == s or h.endswith("." + s) for s in skip):
            app_hosts.add(h)
            app_hosts.add(domainmatch.registrable(h))

    if not app_hosts:
        unresolved.append({**r, "reason": "no_url_data"})
        continue

    candidate_ids = {r["live_facility_id"]}
    if r.get("fresh_facility_id"):
        candidate_ids.add(r["fresh_facility_id"])
    for c in r.get("other_candidates", []):
        if c.get("facility_id"):
            candidate_ids.add(int(c["facility_id"]))

    matches = {fid for fid in candidate_ids
              if fac_hosts.get(fid) and app_hosts & fac_hosts[fid]}

    if r["live_facility_id"] in matches:
        confirmed.append(r)
    elif len(matches) == 1:
        corrected.append({**r, "correct_facility_id": next(iter(matches))})
    elif len(matches) > 1:
        unresolved.append({**r, "reason": "domain_matches_multiple_candidates"})
    elif any(h.endswith(_NON_US_TLDS) for h in app_hosts):
        # The app's only domain evidence is non-US. Neither the live link nor any
        # US candidate is likely correct -- this is probably a Canadian/UK club
        # wrongly linked to a same-named US facility, not a case where the "right"
        # US alternative just needs finding. Flag for likely REMOVAL, not re-match.
        unresolved.append({**r, "reason": "app_domain_is_non_us",
                          "app_hosts": sorted(app_hosts)})
    else:
        unresolved.append({**r, "reason": "domain_matches_none_of_the_candidates",
                          "app_hosts": sorted(app_hosts)})

print(f"\nconfirmed by domain    : {len(confirmed)}")
print(f"corrected by domain    : {len(corrected)}")
print(f"no domain signal (human): {len(unresolved)}")

# ---- apply corrections -----------------------------------------------------------
print("\n--- applying corrections ---")
for r in corrected:
    fid = r["correct_facility_id"]
    old = db.query(f"SELECT facility_name FROM facility WHERE facility_id={r['live_facility_id']}")
    new = db.query(f"SELECT facility_name FROM facility WHERE facility_id={fid}")
    old_name = old[0]["facility_name"] if old else "?"
    new_name = new[0]["facility_name"] if new else "?"
    print(f"  {r['app_name'][:32]:34s} {old_name[:26]:28s} -> {new_name[:26]:28s} "
         f"(#{r['live_facility_id']} -> #{fid})")
    db.exec_sql(f"DELETE FROM ga_facility_app WHERE track_id={r['track_id']} "
               f"AND facility_id={r['live_facility_id']}")
    db.upsert("ga_facility_app", [{
        "facility_id": fid, "track_id": r["track_id"], "match_confidence": 0.95,
        "match_method": "domain",
        "match_status": f"Corrected via Phase 2 domain cross-check "
                        f"({r['live_facility_id']} -> {fid})",
    }], on_conflict="facility_id,track_id")

with open("docs/phase2_domain_crosscheck.json", "w") as f:
    json.dump({"confirmed": confirmed, "corrected": corrected,
              "unresolved": unresolved}, f, indent=2, default=str)
print(f"\nwrote docs/phase2_domain_crosscheck.json")
print(f"\nremaining for human review: {len(unresolved)} (down from {len(flagged)})")
