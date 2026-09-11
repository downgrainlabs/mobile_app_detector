import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, domainmatch, lens

facs = domainmatch.facility_hosts()
rows = db.query("SELECT track_id, query_used, raw, matcher_facility_id FROM ga_lens_detection WHERE raw IS NOT NULL")
print(f"reprocessing {len(rows)} cached responses with country-aware ordered walk", flush=True)

updates = []
by_country = {}
domain_changed, country_blocked = [], []
for r in rows:
    out = lens.resolve_ordered(r["raw"] or {}, facs)
    country = out["country"]
    by_country[country] = by_country.get(country, 0) + 1
    province = out["city_state"]["state"] if (out["city_state"] and not out["city_state"]["is_us_state"]) else None

    updates.append({
        "track_id": r["track_id"],
        "detected_country": country,
        "detected_province": province,
    })

    dh = out["domain"]
    if country not in (None, "US") and dh is None:
        # would have been considered before country-locking existed
        country_blocked.append((r, out))

db.upsert("ga_lens_detection", updates, on_conflict="track_id")

print("\nby detected country:")
for k, v in sorted(by_country.items(), key=lambda kv: -kv[1]):
    print(f"  {str(k):10s} {v}")

print(f"\n{len(country_blocked)} apps where a non-US country was locked "
     f"(these correctly get NO domain/facility match against our US-only table):")
for r, out in country_blocked:
    ce = out["country_evidence"] or {}
    print(f"  {r['track_id']:>12}  country={out['country']:8s} "
         f"evidence: {ce.get('via','?')} (rank {ce.get('rank','?')})")

with open("docs/lens_country_reprocess.json", "w") as f:
    json.dump({"by_country": by_country,
              "country_blocked": [{"track_id": r["track_id"],
                                   "country": out["country"],
                                   "evidence": out["country_evidence"]}
                                  for r, out in country_blocked]}, f, indent=2)
print("\nwrote docs/lens_country_reprocess.json")
