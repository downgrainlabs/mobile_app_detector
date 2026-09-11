"""Cross-check every app currently in Needs Linking or Collision Residue
against Derek's Canada_reference.csv (2205 courses, own website_url) BEFORE
any match-service/facility-name resolution runs. A domain hit means the app
IS that Canadian course -- no amount of US-side name/state tuning fixes a
country-of-origin problem, since Canadian courses routinely share a name with
a US course ("Westwood Country Club", "Highlands Golf Club").

Sets ga_app.canadian_course_at (permanent -- join.py's candidate query and
the needs-linking export both already exclude it), and for residue apps,
removes the wrong US link the same way the earlier non-US removals did.
"""
import sys, os, csv, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, domainmatch

canada_hosts = domainmatch.load_canada_hosts()
print(f"indexed {len(canada_hosts)} Canadian course hosts")
skip = domainmatch.vendor_hosts()


def app_canada_hit(a):
    for field in ("seller_url", "support_url", "privacy_policy_url", "developer_website"):
        h = domainmatch.host(a.get(field))
        if not h or any(h == s or h.endswith("." + s) for s in skip):
            continue
        for key in (h, domainmatch.registrable(h)):
            hits = canada_hosts.get(key)
            if hits:
                return hits[0], key
    return None, None


needs_linking_ids = {r["track_id"] for r in db.query("""
    SELECT a.track_id FROM ga_app a
    JOIN ga_app_vendor v ON v.track_id = a.track_id AND v.vendor IS NOT NULL
    LEFT JOIN ga_facility_app fa ON fa.track_id = a.track_id
    WHERE a.delisted_at IS NULL AND a.not_a_golf_course_at IS NULL
      AND a.canadian_course_at IS NULL AND fa.track_id IS NULL
""")}
residue_ids = set()
with open("docs/phase2_residue_v3.csv", encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        residue_ids.add(int(row["track_id"]))

all_ids = sorted(needs_linking_ids | residue_ids)
print(f"{len(needs_linking_ids)} needs-linking + {len(residue_ids)} residue "
     f"= {len(all_ids)} apps to check")

apps = {}
for i in range(0, len(all_ids), 400):
    chunk = ",".join(str(t) for t in all_ids[i:i + 400])
    for r in db.query(f"SELECT track_id, track_name, seller_url, support_url, "
                      f"privacy_policy_url, developer_website FROM ga_app "
                      f"WHERE track_id IN ({chunk})"):
        apps[r["track_id"]] = r

hits = []
for tid, a in apps.items():
    course, matched_host = app_canada_hit(a)
    if course:
        hits.append({"track_id": tid, "app_name": a["track_name"],
                     "matched_host": matched_host, "canada_course": course,
                     "in_residue": tid in residue_ids,
                     "in_needs_linking": tid in needs_linking_ids})

print(f"\n{len(hits)} apps matched a Canadian course's own domain")
for h in hits:
    pop = "residue" if h["in_residue"] else "needs_linking"
    print(f"  [{pop:13s}] {h['track_id']:12d} {h['app_name']!r:35s} -> "
         f"{h['matched_host']:25s} {h['canada_course']['course_name']} "
         f"({h['canada_course']['city']}, {h['canada_course']['province']})")

with open("docs/canada_domain_sweep.json", "w") as f:
    json.dump(hits, f, indent=2, default=str)

# ---- apply: flag permanently, and drop the wrong US link if one exists ------
for h in hits:
    tid = h["track_id"]
    note = f"{h['canada_course']['course_name']} ({h['matched_host']})"
    db.exec_sql(f"UPDATE ga_app SET canadian_course_at = now(), "
               f"canadian_course_match = '{note.replace(chr(39), chr(39)+chr(39))}' "
               f"WHERE track_id = {tid}")
    if h["in_residue"]:
        db.exec_sql(f"DELETE FROM ga_facility_app WHERE track_id = {tid}")

print(f"\nflagged {len(hits)} apps as Canadian; removed wrong US links for "
     f"{sum(1 for h in hits if h['in_residue'])} residue apps")
