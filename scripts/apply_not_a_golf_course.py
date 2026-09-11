"""Read a labeled needs-linking CSV and permanently exclude every row marked
not_a_golf_course -- writes an 'exclude' row to app_crosswalk, the durable
manual-override table checked before match-service ever runs (join.py's
apply_crosswalk()), so these never resurface in a future run. Also sets the
older not_a_golf_course_at column for backward compatibility with exports
still filtering on it directly.

Usage: python scripts/apply_not_a_golf_course.py docs/phase2_needs_linking_v4.csv
"""
import sys, os, csv
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db

path = sys.argv[1] if len(sys.argv) > 1 else "docs/phase2_needs_linking_v4.csv"
rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))

flagged = [r for r in rows if (r.get("not_a_golf_course") or "").strip().lower() in ("y", "yes", "1", "true")]
print(f"{len(flagged)} rows marked not_a_golf_course")

crosswalk_rows = [{
    "track_id": int(r["track_id"]), "link_type": "exclude",
    "exclude_reason": "not_a_golf_course",
    "source": "manual_review",
    "notes": (r.get("NOTES") or r.get("see_notes") or "")[:500] or None,
} for r in flagged]
if crosswalk_rows:
    db.upsert("app_crosswalk", crosswalk_rows, on_conflict="track_id")

ids = [int(r["track_id"]) for r in flagged]
for i in range(0, len(ids), 400):
    chunk = ",".join(str(t) for t in ids[i:i + 400])
    db.exec_sql(f"UPDATE ga_app SET not_a_golf_course_at = now() WHERE track_id IN ({chunk})")

for r in flagged:
    print(f"  {r['track_id']} {r['app_name']}")
print("done")
