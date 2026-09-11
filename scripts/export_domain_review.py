import sys, os, csv, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, domainmatch, lens, vendors

facs = domainmatch.facility_hosts()
reg = vendors.load()
MULTI_TENANT_NAMES = {
    "clubspot country club",   # Clubspot's own sample/demo app
    "foreup golf club",        # foreUP's generic app
    "chronogolf by lightspeed",
    "club prophet stock",
    "teesnap golf course",
    "clubcentral - by foretees", "ft staff", "forecaddie", "ft branded club",
    "just tee times usa",      # aggregator, not one course
    "greatlife golf",          # membership-network brand site, not one club
    "winworks mobile",
}

rows = db.query("""
    SELECT l.track_id, a.track_name, a.artwork_url, l.query_used, l.raw,
           l.matcher_facility_id, l.matcher_status, v.vendor,
           EXISTS (SELECT 1 FROM ga_facility_app fa WHERE fa.track_id = l.track_id) linked
    FROM ga_lens_detection l
    JOIN ga_app a ON a.track_id = l.track_id
    LEFT JOIN ga_app_vendor v ON v.track_id = l.track_id
    WHERE l.raw IS NOT NULL
    ORDER BY l.track_id
""")

out = []
non_us_rows = []
for r in rows:
    resp = r["raw"] or {}
    result = lens.resolve_ordered(resp, facs)
    dh = result["domain"]
    cs = result["city_state"]
    country = result["country"]

    # Confidently non-US: this is a resolved fact, not something needing review. It
    # correctly gets no facility link today (our table is US-only) and the country/
    # province is stored so it matches for free once non-US facilities exist.
    if country not in (None, "US"):
        ce = result["country_evidence"] or {}
        non_us_rows.append({
            "track_id": r["track_id"], "app_name": r["track_name"],
            "detected_country": country,
            "province_or_region": cs["state"] if cs else "",
            "evidence": f"{ce.get('via', '?')} (rank {ce.get('rank', '?')})",
        })
        continue

    flags = []
    name_l = (r["track_name"] or "").lower()
    if name_l in MULTI_TENANT_NAMES:
        flags.append("KNOWN MULTI-TENANT/DEMO APP -- should not link to any single facility")
    v = reg.vendors.get(r["vendor"]) if r["vendor"] else None
    if v and not v.detectable_per_facility:
        flags.append(f"vendor '{r['vendor']}' ships one shared app, per-facility detection unreliable")
    if dh and dh["rank"] > 5:
        flags.append(f"domain hit is deep (rank {dh['rank']}) -- weaker signal, verify carefully")
    GENERIC_HOSTING = ("canva.site", "wixsite.com", "weebly.com", "sites.google.com",
                       "blogspot.com", "wordpress.com", "carrd.co", "godaddysites.com")
    if dh and any(dh["host"].endswith(g) for g in GENERIC_HOSTING):
        flags.append(f"host is a generic page-builder subdomain ({dh['host']}) -- likely "
                     f"a stale/wrong website_url on OUR facility row, not real confirmation")
    # crude state-in-name vs resolved-state cross-check
    import re
    m = re.search(r"[\-,]\s*([A-Z]{2})\s*$", r["track_name"] or "")
    if m and dh:
        fac_state = db.query(f"SELECT state_code FROM facility WHERE facility_id={dh['facility_id']}")
        fs = fac_state[0]["state_code"] if fac_state else None
        if fs and fs != m.group(1):
            flags.append(f"APP NAME SAYS STATE={m.group(1)} but resolved facility is in {fs} -- likely WRONG")

    out.append({
        "track_id": r["track_id"],
        "app_name": r["track_name"],
        "app_store_url": f"https://apps.apple.com/us/app/id{r['track_id']}",
        "query_used": r["query_used"] or "",
        "domain_hit_host": dh["host"] if dh else "",
        "domain_hit_rank": dh["rank"] if dh else "",
        "proposed_facility": (f"{dh['facility_id']} | {dh['facility_name']}" if dh else ""),
        "city_state_hit": (f"{cs['city']}, {cs['state']} (rank {cs['rank']})" if cs else ""),
        "prior_matcher_status": r["matcher_status"] or "",
        "prior_matcher_facility": r["matcher_facility_id"] or "",
        "currently_linked": "yes" if r["linked"] else "",
        "FLAGS": "; ".join(flags),
        "CORRECT [y/n]": "", "ACTUAL_FACILITY_ID": "", "NOTES": "",
    })

out.sort(key=lambda r: (not bool(r["FLAGS"]), not r["domain_hit_host"], r["track_id"]))

HEADER = ["track_id", "app_name", "app_store_url", "query_used",
          "domain_hit_host", "domain_hit_rank", "proposed_facility", "city_state_hit",
          "prior_matcher_status", "prior_matcher_facility", "currently_linked", "FLAGS",
          "CORRECT [y/n]", "ACTUAL_FACILITY_ID", "NOTES"]
path = "docs/lens_domain_review.csv"
with open(path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=HEADER)
    w.writeheader()
    w.writerows(out)

non_us_path = "docs/lens_non_us.csv"
with open(non_us_path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["track_id", "app_name", "detected_country",
                                      "province_or_region", "evidence"])
    w.writeheader()
    w.writerows(non_us_rows)

print(json.dumps({
    "review_path": path, "review_rows": len(out),
    "with_domain_hit": sum(1 for r in out if r["domain_hit_host"]),
    "flagged": sum(1 for r in out if r["FLAGS"]),
    "non_us_path": non_us_path, "non_us_rows": len(non_us_rows),
}, indent=2))
