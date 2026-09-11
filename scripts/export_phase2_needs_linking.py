"""Export the 'Needs Linking' population -- apps carrying a known vendor
fingerprint with zero facility link at all. Filtered through lens.classify()
(vendor signature already required by the query below; non-US and
golf/tee-time-signal are applied here) so this file only ever contains
genuine candidates -- not apps that were never going to be pursued further
anyway. Excluded rows go to a companion file with their reason, not silently
dropped.

Derek triages each remaining row into:
  needs_lens        -- plausibly a real course app, name/city too weak for
                       match-service to resolve; a reverse-image Lens lookup
                       is the next lever
  not_a_golf_course -- vendor fingerprint fired, but this isn't a specific
                       course/club app (e.g. a vendor's own demo/admin app,
                       or a non-course property on a shared platform). Apply
                       these with scripts/apply_not_a_golf_course.py -- it
                       sets ga_app.not_a_golf_course_at, which join.py's
                       candidate query and this export both already respect,
                       so labeled apps drop out of every future run for good.
  see_notes         -- anything else; freeform NOTES field for follow-up
"""
import sys, os, csv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, lens

rows = db.query("""
    SELECT a.track_id, a.track_name, a.description, a.subtitle, v.vendor,
           a.seller_url, a.support_url, a.privacy_policy_url, a.developer_website
    FROM ga_app a
    JOIN ga_app_vendor v ON v.track_id = a.track_id AND v.vendor IS NOT NULL
    LEFT JOIN ga_facility_app fa ON fa.track_id = a.track_id
    LEFT JOIN app_crosswalk cw ON cw.track_id = a.track_id
    WHERE a.delisted_at IS NULL AND a.not_a_golf_course_at IS NULL
      AND a.canadian_course_at IS NULL AND fa.track_id IS NULL
      AND cw.track_id IS NULL
    ORDER BY v.vendor, a.track_name
""")
print(f"{len(rows)} vendor-confirmed apps with no facility link, before classify()")


def urls(r):
    vals = [r.get(k) for k in ("seller_url", "support_url", "privacy_policy_url",
                               "developer_website") if r.get(k)]
    return " | ".join(vals)


needs_linking, excluded = [], []
for r in rows:
    reason = lens.classify(r, r["vendor"])
    row = {
        "track_id": r["track_id"],
        "app_name": r["track_name"],
        "vendor": r["vendor"],
        "app_store_url": f"https://apps.apple.com/us/app/id{r['track_id']}",
        "app_urls": urls(r),
        "description_snippet": (r.get("description") or "")[:200].replace("\n", " "),
    }
    if reason:
        row["exclude_reason"] = reason
        excluded.append(row)
    else:
        row.update({"needs_lens": "", "not_a_golf_course": "", "see_notes": "", "NOTES": ""})
        needs_linking.append(row)

print(f"{len(needs_linking)} remain after classify(); {len(excluded)} excluded "
     f"(no vendor was already filtered by the query -- these are non-US/no-golf-signal)")

HEADER = ["track_id", "app_name", "vendor", "app_store_url", "app_urls",
          "description_snippet", "needs_lens", "not_a_golf_course", "see_notes", "NOTES"]
path = "docs/phase2_needs_linking_v6.csv"
with open(path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=HEADER)
    w.writeheader()
    w.writerows(needs_linking)
print(f"wrote {path}: {len(needs_linking)} rows")

EXCLUDED_HEADER = ["track_id", "app_name", "vendor", "app_store_url", "app_urls",
                   "description_snippet", "exclude_reason"]
excluded_path = "docs/phase2_needs_linking_excluded.csv"
with open(excluded_path, "w", encoding="utf-8-sig", newline="") as f:
    w = csv.DictWriter(f, fieldnames=EXCLUDED_HEADER)
    w.writeheader()
    w.writerows(excluded)
print(f"wrote {excluded_path}: {len(excluded)} rows")
