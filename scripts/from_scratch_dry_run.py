"""Full from-scratch dry run, per Derek (2026-08-20): reuse the raw sweep
corpus (ga_app) as-is, but redo everything downstream using ONLY the current
codebase's rules -- no trust in any individual determination made earlier
this session (existing ga_facility_app links, prior manual corrections,
cached Lens results). Writes two CSVs; does NOT touch the real
ga_facility_app table, so the actual production state is untouched while
this validates the rules.

Steps:
  1. Reset canadian_course_at (re-derive fresh via the domain sweep, not
     trusted from before). not_a_golf_course_at is already all-NULL (never
     applied) and app_crosswalk is already empty -- confirmed, nothing to
     reset there.
  2. Re-run vendor labeling (enrich.py) fresh against current vendors.yaml.
  3. Re-run the Canada domain sweep against the FULL vendor-confirmed pool
     (not just needs-linking/residue -- this run treats nothing as resolved).
  4. Build the match-service candidate list via join.py's CURRENT
     candidate_name()/normalize_state() (all this session's extraction
     fixes included), submit with include_candidates=True so collisions
     are visible in the same pass.
  5. Classify: unambiguous in-scope match -> Looks Good. No match, rejected,
     or a same-confidence competing candidate -> Needs Lens.
"""
import sys, os, csv, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, join, config, domainmatch, enrich

t0 = time.time()

print("=== step 1: reset canadian_course_at ===", flush=True)
db.exec_sql("UPDATE ga_app SET canadian_course_at = NULL, canadian_course_match = NULL "
           "WHERE canadian_course_at IS NOT NULL")

print("=== step 2: re-run vendor labeling (enrich.py) ===", flush=True)
enrich_stats = enrich.run(escalate=True, html_for_matching=True)
print(json.dumps(enrich_stats.as_dict(), indent=2, default=str), flush=True)
print(f"  elapsed so far: {time.time()-t0:.0f}s", flush=True)

print("=== step 3: fresh Canada domain sweep (full vendor-confirmed pool) ===", flush=True)
canada_hosts = domainmatch.load_canada_hosts()
skip = domainmatch.vendor_hosts()
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
print(f"  {len(candidate_apps)} apps in the candidate pool (matches join.py's own query)")

canada_hits = []
for a in candidate_apps:
    for field in ("seller_url", "support_url", "privacy_policy_url", "developer_website"):
        h = domainmatch.host(a.get(field))
        if not h or any(h == s or h.endswith("." + s) for s in skip):
            continue
        for key in (h, domainmatch.registrable(h)):
            hit = canada_hosts.get(key)
            if hit:
                canada_hits.append((a["track_id"], hit[0]))
                break
        else:
            continue
        break

print(f"  {len(canada_hits)} apps flagged Canadian by domain")
for i in range(0, len(canada_hits), 400):
    chunk = canada_hits[i:i + 400]
    for tid, course in chunk:
        note = f"{course['course_name']} ({course.get('city')}, {course.get('province')})".replace("'", "''")
        db.exec_sql(f"UPDATE ga_app SET canadian_course_at = now(), "
                   f"canadian_course_match = '{note}' WHERE track_id = {tid}")
canada_flagged_ids = {tid for tid, _ in canada_hits}
print(f"  elapsed so far: {time.time()-t0:.0f}s", flush=True)

print("=== step 4: build match-service candidates (current rules only) ===", flush=True)
not_golf_ids = {r["track_id"] for r in db.query(
    "SELECT track_id FROM ga_app WHERE not_a_golf_course_at IS NOT NULL")}
crosswalk_ids = {r["track_id"] for r in db.query("SELECT track_id FROM app_crosswalk")}
print(f"  not_a_golf_course excluded: {len(not_golf_ids)} | crosswalk excluded: {len(crosswalk_ids)}")

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

print(f"  {len(items)} apps to submit to match-service")

in_scope = {r["facility_id"] for r in db.query(
    f"SELECT facility_id FROM facility WHERE {config.FACILITY_WHERE}")}
print(f"  {len(in_scope)} in-scope facilities")

looks_good, needs_lens = [], []
BATCH = config.MATCH_BATCH_SIZE
for i in range(0, len(items), BATCH):
    chunk = items[i:i + BATCH]
    ids = index[i:i + BATCH]
    r = __import__("requests").post(
        f"{config.MATCH_API_BASE}/match/facility/bulk",
        json={"confidence_threshold": config.MATCH_CONFIDENCE_THRESHOLD,
              "include_candidates": True, "requests": chunk},
        timeout=180)
    r.raise_for_status()
    job_id = r.json()["job_id"]
    deadline = time.time() + 3600
    while time.time() < deadline:
        rs = __import__("requests").get(
            f"{config.MATCH_API_BASE}/match/facility/bulk/{job_id}/status", timeout=120)
        rs.raise_for_status()
        st = rs.json().get("status")
        if st == "completed":
            break
        if st == "failed":
            raise RuntimeError(f"match job {job_id} failed")
        time.sleep(config.MATCH_POLL_INTERVAL_S)
    else:
        raise TimeoutError(f"match job {job_id} timed out")
    rr = __import__("requests").get(
        f"{config.MATCH_API_BASE}/match/facility/bulk/{job_id}/results",
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
        # collision: 2+ candidates at/near the top confidence
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

# apps that never even got submitted (no usable name) also need Lens
submitted_ids = set(index)
for a in candidate_apps:
    tid = a["track_id"]
    if tid in canada_flagged_ids or tid in not_golf_ids or tid in crosswalk_ids:
        continue
    if tid not in submitted_ids:
        needs_lens.append({"track_id": tid, "reason": "no_usable_name",
                           "match_status": "", "submitted_name": "", "submitted_city": "",
                           "submitted_state": "", "candidate_count": 0})

print(f"\n=== done: {len(looks_good)} looks_good, {len(needs_lens)} needs_lens "
     f"(elapsed {time.time()-t0:.0f}s) ===", flush=True)

with open("docs/from_scratch_looks_good.json", "w") as f:
    json.dump(looks_good, f, indent=2, default=str)
with open("docs/from_scratch_needs_lens.json", "w") as f:
    json.dump(needs_lens, f, indent=2, default=str)

# ---- CSVs ---------------------------------------------------------------
fac_ids = {r["facility_id"] for r in looks_good}
facs = {}
ids = list(fac_ids)
for i in range(0, len(ids), 400):
    chunk = ",".join(str(x) for x in ids[i:i + 400])
    for f in db.query(f"SELECT facility_id, facility_name, city, state_code FROM facility "
                      f"WHERE facility_id IN ({chunk})"):
        facs[f["facility_id"]] = f

app_ids = [r["track_id"] for r in looks_good] + [r["track_id"] for r in needs_lens]
apps = {}
for i in range(0, len(app_ids), 400):
    chunk = ",".join(str(t) for t in app_ids[i:i + 400])
    for r in db.query(f"SELECT track_id, track_name FROM ga_app WHERE track_id IN ({chunk})"):
        apps[r["track_id"]] = r["track_name"]

with open("docs/from_scratch_looks_good.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["track_id", "app_name", "app_store_url", "facility_id", "facility_name",
               "city", "state", "match_confidence", "match_status"])
    for r in looks_good:
        fac = facs.get(r["facility_id"], {})
        w.writerow([r["track_id"], apps.get(r["track_id"], ""),
                   f"https://apps.apple.com/us/app/id{r['track_id']}", r["facility_id"],
                   fac.get("facility_name", ""), fac.get("city", ""), fac.get("state_code", ""),
                   r["match_confidence"], r["match_status"]])

with open("docs/from_scratch_needs_lens.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["track_id", "app_name", "app_store_url", "reason", "match_status",
               "submitted_name", "submitted_city", "submitted_state", "candidate_count"])
    for r in needs_lens:
        w.writerow([r["track_id"], apps.get(r["track_id"], ""),
                   f"https://apps.apple.com/us/app/id{r['track_id']}", r["reason"],
                   r["match_status"], r["submitted_name"], r["submitted_city"],
                   r["submitted_state"], r["candidate_count"]])

print("wrote docs/from_scratch_looks_good.csv and docs/from_scratch_needs_lens.csv")
