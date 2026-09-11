"""Rebuild every lens-sourced link from cached raw data using resolve_ordered --
the domain-first, country-locked, collision-aware logic -- instead of the older
city/state-extraction-plus-name-fuzzy-match path that actually created the 12
'lens_matcher' links live in ga_facility_app today.

No new SerpAPI credits: everything here reads ga_lens_detection.raw, which is
already on disk for all processed track_ids. Match-service-2-0 calls (Stage B
fallback) are free/unlimited, unlike Lens.

Decision order per app:
  1. Exactly one domain candidate, country US/undetermined -> HIGH CONFIDENCE,
     link directly as match_method='lens_domain'. This is the strongest evidence
     available (an exact facility.website_url match), stronger than fuzzy name
     matching.
  2. >1 distinct domain candidates -> genuine name collision (Ridgewood TX vs CT).
     Do not guess. No link; recorded as ambiguous for human review.
  3. Country confidently non-US -> no link (correct: our table is US-only).
     Country/province already persisted for when non-US facilities exist.
  4. No domain signal, country US/undetermined -> fall back to match-service with
     the cleaned app name + extracted city/state, exactly as before.
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, domainmatch, join, lens, config

facs = domainmatch.facility_hosts()
in_scope = {r["facility_id"] for r in db.query(
    f"SELECT facility_id FROM facility WHERE {config.FACILITY_WHERE}")}

rows = db.query("""
    SELECT l.track_id, l.query_used, l.raw, a.track_name
    FROM ga_lens_detection l JOIN ga_app a ON a.track_id = l.track_id
    WHERE l.raw IS NOT NULL
    ORDER BY l.track_id
""")
print(f"rebuilding resolution for {len(rows)} cached apps", flush=True)

direct_links, collisions, non_us, fallback_needed = [], [], [], []
for r in rows:
    out = lens.resolve_ordered(r["raw"] or {}, facs)
    dh, dcands, country = out["domain"], out.get("domain_candidates") or [], out["country"]

    if dh:
        direct_links.append((r, dh))
    elif len(dcands) > 1:
        collisions.append((r, dcands))
    elif country not in (None, "US"):
        non_us.append((r, country, out["city_state"]))
    else:
        fallback_needed.append((r, out["city_state"]))

print(f"\n  direct domain links (unambiguous):  {len(direct_links)}")
print(f"  collisions (need human pick):        {len(collisions)}")
print(f"  confidently non-US (no link, correct): {len(non_us)}")
print(f"  no domain signal -> matcher fallback: {len(fallback_needed)}")

# ---- apply the direct, unambiguous domain links --------------------------------
links = [{"facility_id": dh["facility_id"], "track_id": r["track_id"],
         "match_confidence": 0.95, "match_method": "lens_domain",
         "match_status": f"Lens domain match ({dh['host']}, rank {dh['rank']})"}
        for r, dh in direct_links]

# ---- Stage B fallback: match-service for apps with no domain signal ------------
if fallback_needed:
    items, ids = [], []
    for r, cs in fallback_needed:
        item = {"facility_name": r["query_used"] or r["track_name"]}
        if cs:
            item["city"] = cs["city"]
            code = join.normalize_state(cs["state"])
            if code:
                item["state"] = code
        items.append(item)
        ids.append(r["track_id"])
    job = join._submit(items, config.MATCH_CONFIDENCE_THRESHOLD)
    results = join._poll(job, total=len(items))
    assert len(results) == len(items)
    n_matched = sum(1 for res in results if res.get("facility_id"))
    if n_matched < len(items) * 0.10:
        # A prior run silently returned 33/40 No Match where a same-day re-submit of
        # the identical batch matched correctly -- a transient match-service hiccup
        # (Render free-tier cold start), not a code bug, but it produced wrong
        # results without any error. A near-zero match rate is the signature; refuse
        # to trust it rather than silently deleting good links for bad ones.
        raise RuntimeError(
            f"match-service returned only {n_matched}/{len(items)} matches -- "
            f"this looks like a transient service failure, not real No Match results. "
            f"Refusing to apply. Wait and re-run.")
    for tid, res in zip(ids, results):
        fid = res.get("facility_id")
        if fid and int(fid) in in_scope:
            links.append({"facility_id": int(fid), "track_id": tid,
                          "match_confidence": res.get("confidence"),
                          "match_method": "lens_matcher",
                          "match_status": f"Lens+matcher fallback - {res.get('matchStatus')}"})

# ---- replace prior lens-sourced links with this rebuild, EXCEPT ones a human has
# already verified. A blind DELETE-then-reinsert previously destroyed the confirmed
# Arrowhead link (Derek's own screenshot) the moment the collision detector -- built
# an hour later -- saw a second, much-lower-ranked candidate and refused to guess.
# Human confirmation must outrank a fresh algorithmic pass, permanently.
verified_ids = {r["track_id"] for r in db.query(
    "SELECT track_id FROM ga_facility_app WHERE match_method IN "
    "('lens_matcher','lens_domain') AND match_status ILIKE '%verified by%'")}
if verified_ids:
    print(f"\npreserving {len(verified_ids)} human-verified link(s), not touching them")
links = [l for l in links if l["track_id"] not in verified_ids]

before = db.query("SELECT count(*) n FROM ga_facility_app WHERE match_method IN "
                  "('lens_matcher','lens_domain') AND match_status NOT ILIKE '%verified by%'")[0]["n"]
db.exec_sql("DELETE FROM ga_facility_app WHERE match_method IN "
           "('lens_matcher','lens_domain') AND match_status NOT ILIKE '%verified by%'")
if links:
    db.upsert("ga_facility_app", links, on_conflict="facility_id,track_id")
after = db.query("SELECT count(*) n FROM ga_facility_app WHERE match_method IN "
                 "('lens_matcher','lens_domain')")[0]["n"]

print(f"\nreplaced {before} old lens links with {after} rebuilt links "
     f"({sum(1 for l in links if l['match_method']=='lens_domain')} domain-direct, "
     f"{sum(1 for l in links if l['match_method']=='lens_matcher')} matcher-fallback)")

print("\n--- direct domain links applied ---")
for r, dh in direct_links:
    print(f"  {r['track_name'][:32]:34s} -> #{dh['facility_id']} {dh['facility_name']}"
         f" (via {dh['host']}, rank {dh['rank']})")

print("\n--- collisions needing a human pick ---")
for r, dcands in collisions:
    print(f"  {r['track_name'][:32]:34s}")
    for c in dcands:
        print(f"      #{c['facility_id']} {c['facility_name']} (rank {c['rank']}, {c['host']})")

with open("docs/lens_rebuild.json", "w") as f:
    json.dump({
        "direct_links": [{"track_id": r["track_id"], "app_name": r["track_name"], **dh}
                         for r, dh in direct_links],
        "collisions": [{"track_id": r["track_id"], "app_name": r["track_name"],
                        "candidates": dcands} for r, dcands in collisions],
        "non_us": [{"track_id": r["track_id"], "app_name": r["track_name"],
                   "country": c, "city_state": cs} for r, c, cs in non_us],
    }, f, indent=2, default=str)
print("\nwrote docs/lens_rebuild.json")
