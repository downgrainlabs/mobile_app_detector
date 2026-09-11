"""Complete 'from scratch, up to Lens' pipeline: match_service -> domain ->
subtitle -> OCR, a proper waterfall -- each layer only sees what's left after
the previous one, so nothing gets double-resolved. Whatever's left after all
four IS the real Needs Lens number. Reuses the match_service residual already
computed in docs/from_scratch2_needs_lens.json (same code, no resubmission).
"""
import sys, os, csv, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, domainmatch, subtitle_resolve, screenshot_ocr

t0 = time.time()

needs_lens = json.load(open("docs/from_scratch2_needs_lens.json", encoding="utf-8"))
needs_lens_by_id = {r["track_id"]: r for r in needs_lens}
pool_ids = list(needs_lens_by_id)
print(f"{len(pool_ids)} apps entering the domain+subtitle+OCR layers", flush=True)

all_resolved = []  # list of {track_id, facility_id, match_confidence, match_status, match_method}

# ---- layer: domain ------------------------------------------------------------
print("=== domain layer ===", flush=True)
skip = domainmatch.vendor_hosts()
facs = domainmatch.facility_hosts()
rows = db.query(f"SELECT track_id, seller_url, support_url, privacy_policy_url, developer_website "
                f"FROM ga_app WHERE track_id IN ({','.join(str(t) for t in pool_ids)})")
domain_resolved = []
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
        domain_resolved.append({"track_id": r["track_id"], "facility_id": fac["facility_id"],
                                "match_confidence": 0.95, "match_status": "Match - Website Domain",
                                "match_method": "domain"})
print(f"  {len(domain_resolved)} resolved (elapsed {time.time()-t0:.0f}s)", flush=True)
all_resolved.extend(domain_resolved)
pool_ids = [tid for tid in pool_ids if tid not in {r["track_id"] for r in domain_resolved}]

# ---- layer: subtitle ------------------------------------------------------------
print("=== subtitle layer ===", flush=True)
sub_result = subtitle_resolve.run(pool_ids)
for r in sub_result["resolved"]:
    r["match_method"] = "subtitle_state_match"
print(f"  {len(sub_result['resolved'])} resolved (elapsed {time.time()-t0:.0f}s)", flush=True)
all_resolved.extend(sub_result["resolved"])
pool_ids = [tid for tid in pool_ids if tid not in {r["track_id"] for r in sub_result["resolved"]}]

# ---- layer: OCR ------------------------------------------------------------
print("=== OCR layer ===", flush=True)
ocr_result = screenshot_ocr.run(pool_ids)
for r in ocr_result["resolved"]:
    r["match_method"] = "ocr_state_match"
print(f"  {len(ocr_result['resolved'])} resolved (elapsed {time.time()-t0:.0f}s)", flush=True)
all_resolved.extend(ocr_result["resolved"])
pool_ids = [tid for tid in pool_ids if tid not in {r["track_id"] for r in ocr_result["resolved"]}]

print(f"\n=== {len(all_resolved)} total resolved across domain+subtitle+OCR, "
     f"{len(pool_ids)} still need Lens (elapsed {time.time()-t0:.0f}s) ===")

with open("docs/from_scratch_layers_resolved.json", "w") as f:
    json.dump(all_resolved, f, indent=2, default=str)

# ---- merge into final CSVs ---------------------------------------------------
looks_good = json.load(open("docs/from_scratch2_looks_good.json", encoding="utf-8"))
for r in looks_good:
    r.setdefault("match_method", "match_service")
final_looks_good = looks_good + all_resolved
final_needs_lens_ids = pool_ids

print(f"\n=== GRAND TOTAL: {len(final_looks_good)} looks_good, "
     f"{len(final_needs_lens_ids)} needs_lens ===")

fac_ids = {r["facility_id"] for r in final_looks_good}
facs_info = {}
ids = list(fac_ids)
for i in range(0, len(ids), 400):
    chunk = ",".join(str(x) for x in ids[i:i + 400])
    for f in db.query(f"SELECT facility_id, facility_name, city, state_code FROM facility "
                      f"WHERE facility_id IN ({chunk})"):
        facs_info[f["facility_id"]] = f

app_ids = [r["track_id"] for r in final_looks_good] + final_needs_lens_ids
apps = {}
for i in range(0, len(app_ids), 400):
    chunk = ",".join(str(t) for t in app_ids[i:i + 400])
    for r in db.query(f"SELECT track_id, track_name FROM ga_app WHERE track_id IN ({chunk})"):
        apps[r["track_id"]] = r["track_name"]

with open("docs/pipeline_looks_good.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["track_id", "app_name", "app_store_url", "facility_id", "facility_name",
               "city", "state", "match_confidence", "match_status", "match_method"])
    for r in final_looks_good:
        fac = facs_info.get(r["facility_id"], {})
        w.writerow([r["track_id"], apps.get(r["track_id"], ""),
                   f"https://apps.apple.com/us/app/id{r['track_id']}", r["facility_id"],
                   fac.get("facility_name", ""), fac.get("city", ""), fac.get("state_code", ""),
                   r["match_confidence"], r["match_status"], r.get("match_method", "match_service")])

with open("docs/pipeline_needs_lens.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["track_id", "app_name", "app_store_url", "reason", "match_status",
               "submitted_name", "submitted_city", "submitted_state"])
    for tid in final_needs_lens_ids:
        r = needs_lens_by_id.get(tid, {})
        w.writerow([tid, apps.get(tid, ""), f"https://apps.apple.com/us/app/id{tid}",
                   r.get("reason", ""), r.get("match_status", ""), r.get("submitted_name", ""),
                   r.get("submitted_city", ""), r.get("submitted_state", "")])

print("wrote docs/pipeline_looks_good.csv and docs/pipeline_needs_lens.csv")
print(f"total elapsed: {time.time()-t0:.0f}s")
