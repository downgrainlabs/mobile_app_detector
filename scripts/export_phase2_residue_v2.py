"""Rebuild the Phase 2 residue CSV from CURRENT database state -- not the stale
original audit JSON, which no longer reflects corrections/removals/dedup already
applied. Covers everything from the original 551 flagged links that still has no
resolution, split into two clearly different shapes:

  DUPLICATE_LINK   -- the app is STILL linked to two different facilities at once
                      (join.py re-run pollution, cleaned up where domain evidence
                      allowed; these remaining ones have none). A genuine two-way
                      pick, not a single-answer-vs-alternative question.
  NEEDS_REVIEW     -- a single live link with no domain signal strong enough to
                      confirm or correct it automatically.
"""
import sys, os, csv, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db

orig = json.load(open("docs/phase2_collision_audit.json", encoding="utf-8"))
orig_ids = {r["track_id"] for r in orig["disagreements"] + orig["collisions"]}
print(f"original flagged set: {len(orig_ids)} track_ids")

# domain-crosscheck already confirmed these against the live link (match_method
# was left as match_service, not changed to 'domain' -- exclude explicitly so
# already-reviewed rows don't reappear as if unresolved)
crosscheck = json.load(open("docs/phase2_domain_crosscheck.json", encoding="utf-8"))
domain_confirmed_ids = {r["track_id"] for r in crosscheck["confirmed"]}
print(f"already domain-confirmed (excluded): {len(domain_confirmed_ids)}")

# current live state for all of them
live = db.query(f"""
    SELECT track_id, facility_id, match_method, match_status
    FROM ga_facility_app WHERE track_id IN ({','.join(str(t) for t in orig_ids)})
""")
by_track = {}
for r in live:
    by_track.setdefault(r["track_id"], []).append(r)

dup_rows, review_rows = [], []
for tid in sorted(orig_ids):
    links = by_track.get(tid, [])
    if not links:
        continue  # removed (non-US) -- resolved, drop from residue
    if len(links) == 1:
        l = links[0]
        if l["match_method"] in ("domain", "state_hint", "ocr_address"):
            continue  # corrected/resolved via domain, state hint, or OCR -- resolved
        if "confirmed" in (l.get("match_status") or "").lower():
            continue  # confirmed via domain crosscheck or state hint -- resolved
        if tid in domain_confirmed_ids:
            continue  # already confirmed by the domain crosscheck -- resolved
    if len(links) > 1:
        dup_rows.append((tid, links))
    elif links[0]["match_method"] == "match_service":
        review_rows.append((tid, links[0]))

print(f"still duplicate-linked: {len(dup_rows)}")
print(f"still single-link, unresolved: {len(review_rows)}")

app_ids = [tid for tid, _ in dup_rows] + [tid for tid, _ in review_rows]
apps = {r["track_id"]: r for r in db.query(
    f"SELECT track_id, track_name, seller_url, support_url, privacy_policy_url, "
    f"developer_website FROM ga_app WHERE track_id IN ({','.join(str(t) for t in app_ids)})"
)} if app_ids else {}
vendors = {r["track_id"]: (r["vendor"] or "") for r in db.query(
    f"SELECT track_id, vendor FROM ga_app_vendor WHERE track_id IN "
    f"({','.join(str(t) for t in app_ids)})"
)} if app_ids else {}

fac_ids = set()
for _, links in dup_rows:
    fac_ids.update(l["facility_id"] for l in links)
for _, l in review_rows:
    fac_ids.add(l["facility_id"])
facs = {}
ids = list(fac_ids)
for i in range(0, len(ids), 400):
    chunk = ",".join(str(x) for x in ids[i:i + 400])
    for f in db.query(f"SELECT facility_id, facility_name, city, state_code, website_url "
                      f"FROM facility WHERE facility_id IN ({chunk})"):
        facs[f["facility_id"]] = f


def fac_label(fid):
    f = facs.get(fid)
    if not f:
        return str(fid)
    site = f" [{f['website_url']}]" if f.get("website_url") else ""
    return f"{fid} | {f['facility_name']} | {f['city']}, {f['state_code']}{site}"


def app_url_summary(tid):
    a = apps.get(tid, {})
    urls = [a.get(k) for k in ("seller_url", "support_url", "privacy_policy_url",
                               "developer_website") if a.get(k)]
    return " | ".join(urls)


rows = []
for tid, links in dup_rows:
    a = apps.get(tid, {})
    rows.append({
        "category": "DUPLICATE_LINK", "track_id": tid,
        "app_name": a.get("track_name", ""),
        "vendor": vendors.get(tid, ""),
        "app_store_url": f"https://apps.apple.com/us/app/id{tid}",
        "app_urls": app_url_summary(tid),
        "option_A": fac_label(links[0]["facility_id"]),
        "option_B": fac_label(links[1]["facility_id"]) if len(links) > 1 else "",
        "extra_options": "; ".join(fac_label(l["facility_id"]) for l in links[2:]),
        "CORRECT_FACILITY_ID": "", "REMOVE_BOTH [y]": "", "NOTES": "",
    })
for tid, l in review_rows:
    a = apps.get(tid, {})
    rows.append({
        "category": "NEEDS_REVIEW", "track_id": tid,
        "app_name": a.get("track_name", ""),
        "vendor": vendors.get(tid, ""),
        "app_store_url": f"https://apps.apple.com/us/app/id{tid}",
        "app_urls": app_url_summary(tid),
        "option_A": fac_label(l["facility_id"]),
        "option_B": "", "extra_options": "",
        "CORRECT_FACILITY_ID": "", "REMOVE_BOTH [y]": "", "NOTES": "",
    })

rows.sort(key=lambda r: (r["category"], r["vendor"] == "", r["vendor"], r["track_id"]))

HEADER = ["category", "track_id", "app_name", "vendor", "app_store_url", "app_urls",
          "option_A", "option_B", "extra_options",
          "CORRECT_FACILITY_ID", "REMOVE_BOTH [y]", "NOTES"]
path = "docs/phase2_residue_v3.csv"
with open(path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=HEADER)
    w.writeheader()
    w.writerows(rows)

print(json.dumps({"path": path, "rows": len(rows),
                  "duplicate_link": len(dup_rows), "needs_review": len(review_rows)},
                 indent=2))
