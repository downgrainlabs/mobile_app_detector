"""chronogolf_subtitle_resolve.py fetched subtitle for all 113 then-unresolved
Chronogolf apps, but only ever ACTED on the ones that parsed to a US state --
the negative case (subtitle clearly says a Canadian province, England,
France, UAE...) was silently left alone, still fully exposed to Lens/
match-service. That's the actual bug Derek caught (2026-08-19): "River Bend
Golf Club" clearly said "Red Deer, Alberta" in cached data from that same
run, and nothing ever excluded it -- it sat in Needs Linking and got a wrong
Lens match to a Texas facility in the pilot sample.

Zero new API calls -- reprocesses docs/chronogolf_subtitles.json.
"""
import sys, os, re, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from golfapps import db

subs = json.load(open("docs/chronogolf_subtitles.json", encoding="utf-8"))

_CA_PROVINCES = {
    "british columbia": "BC", "ontario": "ON", "nova scotia": "NS",
    "alberta": "AB", "new brunswick": "NB", "saskatchewan": "SK",
    "quebec": "QC", "manitoba": "MB", "prince edward island": "PE",
    "newfoundland and labrador": "NL", "newfoundland": "NL", "yukon": "YT",
    "northwest territories": "NT", "nunavut": "NU",
}
_OTHER_COUNTRIES = {
    "england": "UK", "scotland": "UK", "wales": "UK", "dublin": "Ireland",
    "cork": "Ireland", "clare": "Ireland", "cape town": "South Africa",
    "abu dhabi": "UAE", "dubai": "UAE", "doha": "Qatar", "kigali": "Rwanda",
    "muscat": "Oman", "mauritius": "Mauritius", "guerrero": "Mexico",
    "provence-alpes": "France", "poitou-charentes": "France",
    "saare": "Estonia", "melbourne, victoria": "Australia",
    "victoria": "Australia",  # only combined w/ non-BC context below
}
# Saipan/Mariana Islands is a US commonwealth -- NOT foreign, must not be
# flagged even though "Islands" pattern-matches a lot of the above shape.
_US_SAFE = {"saipan", "mariana islands"}


def classify(subtitle):
    if not subtitle:
        return None
    low = subtitle.lower()
    if any(s in low for s in _US_SAFE):
        return None
    if "canada" in low:
        return "Canada"
    for name, code in _CA_PROVINCES.items():
        if re.search(r"\b" + re.escape(name) + r"\b", low) or \
           re.search(r",\s*" + code.lower() + r"\b", low):
            return f"Canada ({code})"
    for name, country in _OTHER_COUNTRIES.items():
        if name in low:
            return country
    return None


results = []
for tid_str, subtitle in subs.items():
    tid = int(tid_str)
    country = classify(subtitle)
    if country:
        results.append((tid, subtitle, country))

print(f"{len(results)} apps clearly non-US per subtitle, never flagged")
for tid, subtitle, country in results:
    print(f"  {tid:12d} {subtitle!r:40s} -> {country}")

# ---- apply: crosswalk exclude + remove any wrong existing link -------------
apps = {r["track_id"]: r["track_name"] for r in db.query(
    f"SELECT track_id, track_name FROM ga_app WHERE track_id IN "
    f"({','.join(str(t) for t,_,_ in results)})"
)}
crosswalk_rows = [{
    "track_id": tid, "link_type": "exclude",
    "exclude_reason": f"non_us:{country}",
    "source": "chronogolf_subtitle_nonus_sweep",
    "notes": f"subtitle={subtitle!r}",
} for tid, subtitle, country in results]
db.upsert("app_crosswalk", crosswalk_rows, on_conflict="track_id")

removed = 0
for tid, subtitle, country in results:
    existing = db.query(f"SELECT facility_id FROM ga_facility_app WHERE track_id={tid}")
    if existing:
        db.exec_sql(f"DELETE FROM ga_facility_app WHERE track_id={tid}")
        removed += len(existing)

print(f"\nflagged {len(results)} apps in app_crosswalk (exclude); "
     f"removed {removed} existing wrong links")
