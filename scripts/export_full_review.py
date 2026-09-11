"""Show the RAW, unassisted engine output for every cached app -- the COMPLETE
two-stage pipeline (Stage A domain-first resolution, Stage B match-service fallback
for apps with no domain signal), not just Stage A alone.

Human overrides (Arrowhead, Austin) are real and stay in the live database for
production use, but they must NOT leak into this evaluation -- the point of this
export is to measure how the algorithm performs on its own, per Derek: "whatever i
manually linked is irrelevant, i want to test the engine raw." So OUR_ANSWER and
FLAGS are always computed fresh from resolve_ordered() + a live Stage-B fallback
call; a prior human decision is shown only as a separate reference column for
context, never substituted into the graded answer.
"""
import sys, os, csv, json, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import config, db, domainmatch, join, lens, vendors

facs = domainmatch.facility_hosts()
reg = vendors.load()
in_scope = {r["facility_id"] for r in db.query(
    f"SELECT facility_id FROM facility WHERE {config.FACILITY_WHERE}")}
MULTI_TENANT_NAMES = {
    "clubspot country club", "foreup golf club", "chronogolf by lightspeed",
    "club prophet stock", "teesnap golf course", "clubcentral - by foretees",
    "ft staff", "forecaddie", "ft branded club", "just tee times usa",
    "greatlife golf", "winworks mobile",
}

rows = db.query("""
    SELECT l.track_id, a.track_name, l.query_used, l.raw,
           l.detected_country, l.detected_province, v.vendor,
           fa.match_status AS link_status,
           EXISTS (SELECT 1 FROM ga_facility_app fa2 WHERE fa2.track_id = l.track_id) linked
    FROM ga_lens_detection l
    JOIN ga_app a ON a.track_id = l.track_id
    LEFT JOIN ga_app_vendor v ON v.track_id = l.track_id
    LEFT JOIN ga_facility_app fa ON fa.track_id = l.track_id
    WHERE l.raw IS NOT NULL
    ORDER BY l.track_id
""")

# ---- pass 1: Stage A (domain-first) for every app -------------------------------
prelim = []
fallback_queue = []
for r in rows:
    resp = r["raw"] or {}
    result = lens.resolve_ordered(resp, facs)
    name_l = (r["track_name"] or "").lower()
    is_demo = name_l in MULTI_TENANT_NAMES
    prelim.append((r, result, is_demo))
    dh, dcands, country = result["domain"], result.get("domain_candidates") or [], result["country"]
    if not is_demo and not dh and len(dcands) <= 1 and country in (None, "US"):
        fallback_queue.append(r)

# ---- pass 2: Stage B (match-service fallback), live, free ----------------------
stage_b = {}
if fallback_queue:
    items, ids = [], []
    for r in fallback_queue:
        cs = lens.resolve_ordered(r["raw"] or {}, facs)["city_state"]
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
    assert len(results) == len(items), (
        f"matcher returned {len(results)} for {len(items)} requests")
    n_matched = sum(1 for res in results if res.get("facility_id"))
    if n_matched < len(items) * 0.10:
        print(f"WARNING: only {n_matched}/{len(items)} Stage-B matches -- looks like "
             f"a transient match-service failure, not real results. Re-run.",
             file=sys.stderr)
    for tid, res in zip(ids, results):
        fid = res.get("facility_id")
        stage_b[tid] = {
            "facility_id": int(fid) if (fid and int(fid) in in_scope) else None,
            "status": res.get("matchStatus"),
            "confidence": res.get("confidence"),
        }

# ---- pass 3: build the CSV -------------------------------------------------------
out = []
for r, result, is_demo in prelim:
    resp = r["raw"] or {}
    dh, cs, country = result["domain"], result["city_state"], result["country"]
    dcands = result.get("domain_candidates") or []

    items_ev = list(resp.get("visual_matches") or []) + list(resp.get("organic_results") or [])
    top5 = []
    for i, it in enumerate(items_ev[:5]):
        host = domainmatch.host(it.get("link")) or "?"
        title = (it.get("title") or "")[:60]
        top5.append(f"{i}. {title} ({host})")
    top5_str = "  |  ".join(top5)

    stage = "A:domain"
    if is_demo:
        answer = "SKIPPED (known demo/multi-tenant app -- no real answer expected)"
        rank = ""
    elif country not in (None, "US"):
        answer = f"NON-US: {country}" + (f" / {cs['state']}" if cs else "")
        rank = (result["country_evidence"] or {}).get("rank", "")
    elif dh:
        answer = f"{dh['facility_id']} | {dh['facility_name']}"
        rank = dh["rank"]
    elif len(dcands) > 1:
        answer = "MULTIPLE CANDIDATES: " + " | ".join(
            f"#{c['facility_id']} {c['facility_name']} (rank {c['rank']}, {c['host']})"
            for c in dcands)
        rank = ""
    elif r["track_id"] in stage_b:
        sb = stage_b[r["track_id"]]
        stage = "B:matcher"
        if sb["facility_id"]:
            fac = db.query(f"SELECT facility_name FROM facility WHERE facility_id={sb['facility_id']}")
            fname = fac[0]["facility_name"] if fac else "?"
            answer = f"{sb['facility_id']} | {fname}"
        else:
            answer = f"no match (matcher: {sb['status']})"
        rank = ""
    else:
        answer = "no match"
        rank = ""

    flags = []
    if is_demo:
        flags.append("multi-tenant/demo app")
    v = reg.vendors.get(r["vendor"]) if r["vendor"] else None
    if v and not v.detectable_per_facility:
        flags.append(f"vendor '{r['vendor']}' has no per-facility listing")
    if len(dcands) > 1:
        flags.append(f"COLLISION: {len(dcands)} distinct facilities matched")
    if dh and dh["rank"] > 5:
        flags.append(f"deep rank ({dh['rank']})")
    GENERIC_HOSTING = ("canva.site", "wixsite.com", "weebly.com", "sites.google.com",
                       "blogspot.com", "wordpress.com", "carrd.co", "godaddysites.com")
    if dh and any(dh["host"].endswith(g) for g in GENERIC_HOSTING):
        flags.append("generic page-builder host")
    m = re.search(r"[\-,]\s*([A-Z]{2})\s*$", r["track_name"] or "")
    if m and dh:
        fs = db.query(f"SELECT state_code FROM facility WHERE facility_id={dh['facility_id']}")
        fs = fs[0]["state_code"] if fs else None
        if fs and fs != m.group(1):
            flags.append(f"app name says {m.group(1)}, facility is in {fs} -- WRONG?")

    is_human_override = "verified by" in (r["link_status"] or "").lower()
    live_state = (f"HUMAN OVERRIDE: {r['link_status']}" if is_human_override
                 else (r["link_status"] or "") if r["linked"] else "")

    out.append({
        "track_id": r["track_id"],
        "app_name": r["track_name"],
        "app_store_url": f"https://apps.apple.com/us/app/id{r['track_id']}",
        "query_used": r["query_used"] or "",
        "top_5_results": top5_str,
        "OUR_ANSWER": answer,
        "resolved_by_stage": stage if answer not in ("no match",) and not answer.startswith(("SKIPPED", "NON-US", "MULTIPLE", "no match")) else "",
        "answer_rank": rank,
        "FLAGS": "; ".join(flags),
        "live_db_state (reference only, ignore for grading)": live_state,
        "CORRECT [y/n]": "",
        "ACTUAL_FACILITY_ID_or_COUNTRY": "",
        "NOTES": "",
    })

out.sort(key=lambda r: (not r["OUR_ANSWER"].startswith("MULTIPLE"),
                        not bool(r["FLAGS"]),
                        r["OUR_ANSWER"].startswith("no match"), r["track_id"]))

HEADER = ["track_id", "app_name", "app_store_url", "query_used", "top_5_results",
          "OUR_ANSWER", "resolved_by_stage", "answer_rank", "FLAGS",
          "live_db_state (reference only, ignore for grading)",
          "CORRECT [y/n]", "ACTUAL_FACILITY_ID_or_COUNTRY", "NOTES"]
path = "docs/lens_raw_engine_review.csv"
with open(path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=HEADER)
    w.writeheader()
    w.writerows(out)

n_domain = sum(1 for r in out if r["resolved_by_stage"] == "A:domain")
n_matcher = sum(1 for r in out if r["resolved_by_stage"] == "B:matcher")
n_collision = sum(1 for r in out if r["OUR_ANSWER"].startswith("MULTIPLE"))
n_nonus = sum(1 for r in out if r["OUR_ANSWER"].startswith("NON-US"))
n_skipped = sum(1 for r in out if r["OUR_ANSWER"].startswith("SKIPPED"))
n_nomatch = sum(1 for r in out if r["OUR_ANSWER"].startswith("no match"))
print(json.dumps({
    "path": path, "rows": len(out),
    "resolved_stage_A_domain": n_domain,
    "resolved_stage_B_matcher": n_matcher,
    "total_resolved": n_domain + n_matcher,
    "collisions_unresolved_by_engine": n_collision,
    "non_us_detected": n_nonus,
    "skipped_demo_apps": n_skipped,
    "true_no_match": n_nomatch,
}, indent=2))
