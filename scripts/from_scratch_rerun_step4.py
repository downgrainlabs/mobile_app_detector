"""Re-run just step 4 (match-service submission) + the domain pass, reusing
the already-computed vendor labels and Canada flags from the first
from_scratch_dry_run.py pass -- only candidate_name()/clean_name() changed
(the Tee Times suffix fix), so steps 1-3 don't need to repeat.
"""
import sys, os, csv, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, join, config, domainmatch

t0 = time.time()

candidate_apps = db.query("""
    SELECT a.track_id, a.track_name, a.description, a.bundle_id, v.vendor,
           a.seller_url, a.support_url, a.privacy_policy_url, a.developer_website
    FROM ga_app a
    LEFT JOIN ga_app_vendor v ON v.track_id = a.track_id
    WHERE a.delisted_at IS NULL
      AND (v.vendor IS NOT NULL OR a.description ILIKE '%tee time%'
           OR a.description ILIKE '%golf club%')
    ORDER BY a.track_id
""")
canada_flagged_ids = {r["track_id"] for r in db.query(
    "SELECT track_id FROM ga_app WHERE canadian_course_at IS NOT NULL")}
not_golf_ids = {r["track_id"] for r in db.query(
    "SELECT track_id FROM ga_app WHERE not_a_golf_course_at IS NOT NULL")}
crosswalk_ids = {r["track_id"] for r in db.query("SELECT track_id FROM app_crosswalk")}
print(f"{len(candidate_apps)} candidate pool | canada={len(canada_flagged_ids)} "
     f"not_golf={len(not_golf_ids)} crosswalk={len(crosswalk_ids)}")

items, index = [], []
for a in candidate_apps:
    tid = a["track_id"]
    if tid in canada_flagged_ids or tid in not_golf_ids or tid in crosswalk_ids:
        continue
    name, city, state = join.candidate_name(a)
    if not name or len(name) < 3:
        continue
    item = {"facility_name": name}
    if city:
        item["city"] = city
    code = join.normalize_state(state)
    if code:
        item["state"] = code
    items.append(item)
    index.append(tid)

print(f"{len(items)} apps to submit to match-service (with the Tee Times fix)")

in_scope = {r["facility_id"] for r in db.query(
    f"SELECT facility_id FROM facility WHERE {config.FACILITY_WHERE}")}

looks_good, needs_lens = [], []
BATCH = config.MATCH_BATCH_SIZE
import requests
for i in range(0, len(items), BATCH):
    chunk = items[i:i + BATCH]
    ids = index[i:i + BATCH]
    r = requests.post(f"{config.MATCH_API_BASE}/match/facility/bulk",
                      json={"confidence_threshold": config.MATCH_CONFIDENCE_THRESHOLD,
                            "include_candidates": True, "requests": chunk}, timeout=180)
    r.raise_for_status()
    job_id = r.json()["job_id"]
    deadline = time.time() + 3600
    while time.time() < deadline:
        rs = requests.get(f"{config.MATCH_API_BASE}/match/facility/bulk/{job_id}/status", timeout=120)
        rs.raise_for_status()
        st = rs.json().get("status")
        if st == "completed":
            break
        if st == "failed":
            raise RuntimeError(f"match job {job_id} failed")
        time.sleep(config.MATCH_POLL_INTERVAL_S)
    else:
        raise TimeoutError(f"match job {job_id} timed out")
    rr = requests.get(f"{config.MATCH_API_BASE}/match/facility/bulk/{job_id}/results",
                      params={"limit": len(chunk)}, timeout=180)
    rr.raise_for_status()
    results = rr.json().get("results", []) or []
    if len(results) != len(chunk):
        raise RuntimeError(f"batch {i}: {len(results)} results for {len(chunk)} requests")

    for tid, req, res in zip(ids, chunk, results):
        status = res.get("matchStatus") or "unknown"
        fid = res.get("facility_id")
        cands = res.get("candidates") or []
        rejected_scope = fid and int(fid) not in in_scope
        if rejected_scope:
            fid = None
        top_conf = max((c.get("confidence", 0) for c in cands), default=0)
        competing = sum(1 for c in cands if c.get("confidence", 0) >= top_conf - 0.001)
        if fid and competing <= 1:
            looks_good.append({"track_id": tid, "facility_id": int(fid),
                               "match_confidence": res.get("confidence"),
                               "match_status": status, "submitted_name": req.get("facility_name"),
                               "submitted_city": req.get("city"), "submitted_state": req.get("state")})
        else:
            needs_lens.append({"track_id": tid, "reason": "collision" if (fid and competing > 1)
                               else ("rejected_out_of_scope" if rejected_scope else "no_match"),
                               "match_status": status, "submitted_name": req.get("facility_name"),
                               "submitted_city": req.get("city"), "submitted_state": req.get("state"),
                               "candidate_count": len(cands)})
    print(f"  batch {i//BATCH}: {i+len(chunk)}/{len(items)} done "
         f"(looks_good={len(looks_good)}, needs_lens={len(needs_lens)}) "
         f"elapsed={time.time()-t0:.0f}s", flush=True)

submitted_ids = set(index)
for a in candidate_apps:
    tid = a["track_id"]
    if tid in canada_flagged_ids or tid in not_golf_ids or tid in crosswalk_ids:
        continue
    if tid not in submitted_ids:
        needs_lens.append({"track_id": tid, "reason": "no_usable_name", "match_status": "",
                           "submitted_name": "", "submitted_city": "", "submitted_state": "",
                           "candidate_count": 0})

print(f"\n=== match-service pass: {len(looks_good)} looks_good, {len(needs_lens)} needs_lens ===")
with open("docs/from_scratch2_looks_good.json", "w") as f:
    json.dump(looks_good, f, indent=2, default=str)
with open("docs/from_scratch2_needs_lens.json", "w") as f:
    json.dump(needs_lens, f, indent=2, default=str)

# ---- domain pass over the residual -------------------------------------------
needs_lens_ids = {r["track_id"] for r in needs_lens}
skip = domainmatch.vendor_hosts()
facs = domainmatch.facility_hosts()
rows = db.query(f"SELECT track_id, seller_url, support_url, privacy_policy_url, developer_website "
                f"FROM ga_app WHERE track_id IN ({','.join(str(t) for t in needs_lens_ids)})")
resolved = []
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

print(f"domain pass: {len(resolved)} more resolved")
resolved_ids = {r["track_id"] for r in resolved}
final_needs_lens = [r for r in needs_lens if r["track_id"] not in resolved_ids]
final_looks_good = looks_good + resolved

print(f"\n=== FINAL: {len(final_looks_good)} looks_good, {len(final_needs_lens)} needs_lens "
     f"(elapsed {time.time()-t0:.0f}s) ===")

fac_ids = {r["facility_id"] for r in final_looks_good}
facs_info = {}
ids = list(fac_ids)
for i in range(0, len(ids), 400):
    chunk = ",".join(str(x) for x in ids[i:i + 400])
    for f in db.query(f"SELECT facility_id, facility_name, city, state_code FROM facility "
                      f"WHERE facility_id IN ({chunk})"):
        facs_info[f["facility_id"]] = f

app_ids = [r["track_id"] for r in final_looks_good] + [r["track_id"] for r in final_needs_lens]
apps = {}
for i in range(0, len(app_ids), 400):
    chunk = ",".join(str(t) for t in app_ids[i:i + 400])
    for r in db.query(f"SELECT track_id, track_name FROM ga_app WHERE track_id IN ({chunk})"):
        apps[r["track_id"]] = r["track_name"]

with open("docs/from_scratch_final_looks_good.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["track_id", "app_name", "app_store_url", "facility_id", "facility_name",
               "city", "state", "match_confidence", "match_status"])
    for r in final_looks_good:
        fac = facs_info.get(r["facility_id"], {})
        w.writerow([r["track_id"], apps.get(r["track_id"], ""),
                   f"https://apps.apple.com/us/app/id{r['track_id']}", r["facility_id"],
                   fac.get("facility_name", ""), fac.get("city", ""), fac.get("state_code", ""),
                   r["match_confidence"], r["match_status"]])

with open("docs/from_scratch_final_needs_lens.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["track_id", "app_name", "app_store_url", "reason", "match_status",
               "submitted_name", "submitted_city", "submitted_state", "candidate_count"])
    for r in final_needs_lens:
        w.writerow([r["track_id"], apps.get(r["track_id"], ""),
                   f"https://apps.apple.com/us/app/id{r['track_id']}", r["reason"],
                   r["match_status"], r["submitted_name"], r["submitted_city"],
                   r["submitted_state"], r["candidate_count"]])
print("wrote docs/from_scratch_final_looks_good.csv and docs/from_scratch_final_needs_lens.csv")
