"""Chronogolf apps carry their location in the App Store Connect 'subtitle'
field ("Manorville, New York"), rendered right under the app title on the
product page but absent from the free iTunes JSON -- confirmed Chronogolf-
specific (Derek, 2026-08-19), so scoped to just this vendor rather than
fetched for the whole corpus.

Covers both populations:
  needs-linking -- fetch subtitle, parse city/state, submit (name, state) to
                   match-service (dry run; pass --apply to write)
  residue       -- fetch subtitle, compare against the CURRENTLY linked
                   facility's own city/state -- confirm or flag mismatch
"""
import sys, os, re, csv, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db, join, config, appstore

APPLY = "--apply" in sys.argv

_SUBTITLE_CITY_STATE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z.'\- ]{1,40}?),\s*([A-Za-z ]{2,20})\s*$")


def parse_subtitle(subtitle):
    if not subtitle:
        return None, None
    m = _SUBTITLE_CITY_STATE_RE.match(subtitle)
    if not m:
        return None, None
    city = m.group(1).strip()
    code = join.normalize_state(m.group(2).strip())
    return (city, code) if code else (None, None)


needs_linking = db.query("""
    SELECT a.track_id, a.track_name FROM ga_app a
    JOIN ga_app_vendor v ON v.track_id = a.track_id AND v.vendor = 'chronogolf'
    LEFT JOIN ga_facility_app fa ON fa.track_id = a.track_id
    WHERE a.delisted_at IS NULL AND a.not_a_golf_course_at IS NULL AND fa.track_id IS NULL
""")
residue_ids = set()
with open("docs/phase2_residue_v3.csv", encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        if row["vendor"] == "chronogolf":
            residue_ids.add(int(row["track_id"]))

all_ids = sorted({a["track_id"] for a in needs_linking} | residue_ids)
print(f"{len(needs_linking)} needs-linking + {len(residue_ids)} residue = {len(all_ids)} chronogolf apps")

client = appstore.AppStoreClient()
subtitles = {}
for i, tid in enumerate(all_ids, 1):
    page = client.fetch(tid)
    if page and page != "__DELISTED__":
        f = appstore.extract(page)
        subtitles[tid] = f.get("subtitle")
    if i % 20 == 0:
        print(f"  fetched {i}/{len(all_ids)}")

with open("docs/chronogolf_subtitles.json", "w") as f:
    json.dump(subtitles, f, indent=2)

parsed = {tid: parse_subtitle(s) for tid, s in subtitles.items()}
n_with_state = sum(1 for c, s in parsed.values() if s)
print(f"\n{n_with_state}/{len(subtitles)} subtitles parsed to a usable state")

# ---- needs-linking: submit to match-service ---------------------------------
by_id = {a["track_id"]: a for a in needs_linking}
items, index = [], []
for tid, a in by_id.items():
    city, state = parsed.get(tid, (None, None))
    if not state:
        continue
    name = join.clean_name(a["track_name"])
    if not name or len(name) < 3:
        continue
    item = {"facility_name": name, "state": state}
    if city:
        item["city"] = city
    items.append(item)
    index.append(tid)

print(f"\nsubmitting {len(items)} needs-linking apps to match-service")
links = []
if items:
    in_scope = {r["facility_id"] for r in db.query(
        f"SELECT facility_id FROM facility WHERE {config.FACILITY_WHERE}")}
    for i in range(0, len(items), config.MATCH_BATCH_SIZE):
        chunk = items[i:i + config.MATCH_BATCH_SIZE]
        ids = index[i:i + config.MATCH_BATCH_SIZE]
        job = join._submit(chunk, config.MATCH_CONFIDENCE_THRESHOLD)
        results = join._poll(job, total=len(chunk))
        for track_id, req, res in zip(ids, chunk, results):
            status = res.get("matchStatus") or "unknown"
            fid = res.get("facility_id")
            if fid and int(fid) not in in_scope:
                fid = None
            if fid:
                links.append({"facility_id": int(fid), "track_id": track_id,
                              "match_confidence": res.get("confidence"),
                              "match_method": "chronogolf_subtitle", "match_status": status,
                              "submitted": req})
    print(f"matched: {len(links)} / {len(items)}")
    for l in links:
        print(f"  {l['track_id']:12d} {l['submitted']!r:55s} -> #{l['facility_id']} "
             f"({l['match_confidence']}) {l['match_status']}")

# ---- residue: compare against the live link ----------------------------------
live_links = {}
for r in db.query(f"SELECT track_id, facility_id FROM ga_facility_app "
                  f"WHERE track_id IN ({','.join(str(t) for t in residue_ids)})" if residue_ids else
                  "SELECT track_id, facility_id FROM ga_facility_app WHERE false"):
    live_links.setdefault(r["track_id"], []).append(r["facility_id"])

fac_ids = {fid for fids in live_links.values() for fid in fids}
facs = {}
if fac_ids:
    for f in db.query(f"SELECT facility_id, facility_name, city, state_code FROM facility "
                      f"WHERE facility_id IN ({','.join(str(x) for x in fac_ids)})"):
        facs[f["facility_id"]] = f

print(f"\nresidue check ({len(residue_ids)} apps):")
residue_report = []
for tid in sorted(residue_ids):
    city, state = parsed.get(tid, (None, None))
    fids = live_links.get(tid, [])
    fac_states = [facs.get(f, {}).get("state_code") for f in fids]
    row = {"track_id": tid, "app_name": by_id.get(tid, {}).get("track_name", ""),
          "subtitle": subtitles.get(tid), "parsed_state": state,
          "live": [(f, facs.get(f, {}).get("facility_name"), facs.get(f, {}).get("state_code"))
                   for f in fids]}
    if state and fids:
        row["verdict"] = "CONFIRM" if state in fac_states else "MISMATCH"
    elif not state:
        row["verdict"] = "no_subtitle_state"
    else:
        row["verdict"] = "no_live_link"
    residue_report.append(row)
    print(f"  {row['verdict']:12s} {tid:12d} subtitle={row['subtitle']!r:30s} "
         f"live={row['live']}")

with open("docs/chronogolf_residue_check.json", "w") as f:
    json.dump(residue_report, f, indent=2, default=str)
with open("docs/chronogolf_needs_linking_results.json", "w") as f:
    json.dump({"links": links}, f, indent=2, default=str)

if APPLY and links:
    touched = sorted({l["track_id"] for l in links})
    for i in range(0, len(touched), 400):
        chunk = ",".join(str(t) for t in touched[i:i + 400])
        db.exec_sql(f"DELETE FROM ga_facility_app WHERE match_method='chronogolf_subtitle' "
                   f"AND track_id IN ({chunk})")
    write_rows = [{k: v for k, v in l.items() if k != "submitted"} for l in links]
    db.upsert("ga_facility_app", write_rows, on_conflict="facility_id,track_id")
    print(f"\napplied {len(write_rows)} needs-linking matches")
else:
    print("\nDRY RUN -- nothing written for needs-linking. Re-run with --apply to write.")
