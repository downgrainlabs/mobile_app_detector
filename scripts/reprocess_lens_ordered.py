import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, domainmatch, lens

facs = domainmatch.facility_hosts()
rows = db.query("""
    SELECT track_id, query_used, raw, matcher_facility_id, matcher_status
    FROM ga_lens_detection WHERE raw IS NOT NULL ORDER BY track_id
""")
print(f"reprocessing {len(rows)} cached responses (visual_matches only -- "
      f"organic_results wasn't persisted in the earlier run)", flush=True)

changes, new_hits, agreements, conflicts = [], [], [], []
for r in rows:
    resp = r["raw"] or {}
    out = lens.resolve_ordered(resp, facs)
    dh = out["domain"]
    old_fid = r["matcher_facility_id"]

    if not dh:
        continue
    new_fid = dh["facility_id"]
    if old_fid is None:
        new_hits.append((r, dh))
    elif old_fid == new_fid:
        agreements.append((r, dh))
    else:
        conflicts.append((r, dh, old_fid))

print(f"\ndomain hits agreeing with prior matcher result: {len(agreements)}")
print(f"NEW domain hits (previously No Match or unresolved): {len(new_hits)}")
print(f"CONFLICTS (domain hit disagrees with prior matcher_facility_id): {len(conflicts)}")

print("\n--- NEW HITS (candidates to link) ---")
for r, dh in new_hits:
    print(f"  {r['track_id']:>12} q={r['query_used']!r:38s} -> "
          f"#{dh['facility_id']} {dh['facility_name']} (via {dh['host']}, rank {dh['rank']})")

print("\n--- CONFLICTS (need a human look) ---")
for r, dh, old_fid in conflicts:
    old = db.query(f"SELECT facility_name, city, state_code FROM facility WHERE facility_id={old_fid}")
    old_name = old[0]["facility_name"] if old else "?"
    print(f"  {r['track_id']:>12} q={r['query_used']!r:38s}")
    print(f"      OLD (matcher): #{old_fid} {old_name}")
    print(f"      NEW (domain) : #{dh['facility_id']} {dh['facility_name']} (via {dh['host']})")

with open("docs/lens_reprocess.json", "w") as f:
    json.dump({
        "new_hits": [{"track_id": r["track_id"], "query": r["query_used"], **dh}
                     for r, dh in new_hits],
        "conflicts": [{"track_id": r["track_id"], "query": r["query_used"],
                       "old_facility_id": old_fid, **dh}
                      for r, dh, old_fid in conflicts],
        "agreements": [{"track_id": r["track_id"]} for r, dh in agreements],
    }, f, indent=2)
print("\nwrote docs/lens_reprocess.json")
