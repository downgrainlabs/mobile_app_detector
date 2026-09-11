"""Phase 2 -- match facilities to apps.

Uses the existing Downgrain bulk match service rather than a fresh rapidfuzz index:
it already resolves facility_other_names and course aliases, which a name-only
matcher would miss.

Direction of the join matters. We do NOT ask "which app does this facility have" for
14k facilities. We take the app corpus, pull the course name each app states in its
own description, and ask the matcher to resolve THAT to a facility. One request per
app in the corpus, not per facility -- and Probe F showed name search misses apps
about 25% of the time, so the corpus->facility direction is the reliable one.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import requests

from . import config, db

log = logging.getLogger(__name__)

# Apps state their course explicitly: "provides tee time booking for X Golf Course
# in Y, ST". Names also sit in the track_name, usually with a suffix to strip.
_DESC_PATTERNS_WITH_LOCATION = [
    re.compile(r"(?i)provides tee time booking for (?P<name>.{3,70}?) in "
               r"(?P<city>[^,]{2,40}),\s*(?P<state>[A-Za-z ]{2,20})"),
    # (?-i:[A-Z]) matters: under the pattern's own (?i) flag, a bare [A-Z]
    # matches lowercase too (Python re quirk), silently defeating this as a
    # "must start with a real capitalized name" guard. Without the scoped
    # override, "For more information on the best golf value in the
    # Yellowstone Valley, call or email the Briarwood today!" matches this
    # pattern as if "more information on the best golf value" were the
    # facility name -- found 2026-08-20, Briarwood Golf Club - MT, which also
    # has a clean trailing-state title the match never got a chance to try
    # (description patterns run before track_name, so a bad description hit
    # doesn't just fail, it PREVENTS the good extraction from running at all).
    re.compile(r"(?i)\bfor (?P<name>(?-i:[A-Z])[\w'&.\- ]{3,60}?(?:Golf|Club|Course|Links|"
               r"Country Club)[\w'&.\- ]{0,20}) in (?P<city>[^,]{2,40}),\s*"
               r"(?P<state>[A-Za-z ]{2,20})"),
]

# Loose prose. Deliberately NOT preferred over track_name -- see candidate_name().
_DESC_PATTERNS_LOOSE = [
    re.compile(r"(?i)welcome to (?P<name>[A-Z][\w'&.\- ]{3,60})"),
]

_NAME_SUFFIXES = re.compile(
    r"(?i)\s*(tee times?|golf tee times?|app|mobile|members?|member app|"
    r"- ?[A-Z]{2}|golf app)\s*$")

# Tokens that belong to the APP's name, never the course's. "Welcome to Dairy Creek
# Golf Course App!" yields "Dairy Creek Golf Course App"; the matcher scores that 0.92
# against the real facility and then rejects it as No Match. One stray token, whole
# link lost.
_JUNK_TAIL = re.compile(
    r"(?i)[\s!.\-–—]*\b(app|apps|application|mobile|ios|android|"
    r"tee times?)\b[\s!.]*$")
_JUNK_LEAD = re.compile(r"(?i)^(the|welcome to|download)\s+")


def clean_name(name: str | None) -> str | None:
    """Strip app-name noise so what we send is a COURSE name."""
    if not name:
        return None
    out = name.strip()
    for _ in range(4):
        prev = out
        out = _JUNK_TAIL.sub("", out).strip()
        out = _JUNK_LEAD.sub("", out).strip()
        out = out.strip(" -–—!.,")
        if out == prev:
            break
    return out or None

# The match service filters on `state` and expects the 2-letter code. Passing the
# spelled-out name is WORSE than passing nothing: 'California' returns a hard No Match
# where omitting state entirely matches exactly. App descriptions almost always spell
# the state out ("...in Thornton, Colorado"), so this normalisation is load-bearing.
STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "washington dc": "DC",
}


_VALID_STATE_CODES = set(STATE_CODES.values()) | {"DC"}
# Course apps often carry their own state as a trailing suffix on track_name, in
# several shapes: "EagleRock Golf Course - MT", "Tempest Golf Club TX" (no dash),
# "Shary Municipal Golf Course-TX" (dash, no space), "Brentwood Country Club -
# Texas" (full name, not a code), "Highlands Country Club (CA)" (parens). Before
# this, that suffix rode along as noise in the name sent to the matcher (no
# state filter applied) instead of being split out into the state field it
# actually is. The boundary must be a dash, whitespace, or start-of-string --
# never bare adjacency to a letter -- so this can't fire mid-word ("Play"
# doesn't end in a state).
_TRAILING_STATE_RE = re.compile(r"(?:^|[-–—\s])([A-Za-z]{2})$")
_PAREN_STATE_RE = re.compile(r"\(\s*([A-Za-z]{2})\s*\)\s*$")
_STATE_NAME_ALTS = sorted(STATE_CODES.keys(), key=len, reverse=True)
_TRAILING_STATE_NAME_RE = re.compile(
    r"(?:^|[-–—\s])(" + "|".join(re.escape(s) for s in _STATE_NAME_ALTS) + r")$",
    re.I)


def strip_trailing_state(name: str) -> tuple[str, str | None]:
    """Split a real trailing state -- code, full name, or (CODE) -- off a name.

    Guarded by _VALID_STATE_CODES so a stray two-letter word (e.g. "- Co" as in
    "Coffee Co") can't be misread as a state.
    """
    m = _PAREN_STATE_RE.search(name)
    if m and m.group(1).upper() in _VALID_STATE_CODES:
        return name[:m.start()].rstrip(" -–—,"), m.group(1).upper()

    m = _TRAILING_STATE_NAME_RE.search(name)
    if m:
        return name[:m.start()].rstrip(" -–—,"), STATE_CODES[m.group(1).lower()]

    m = _TRAILING_STATE_RE.search(name)
    if m and m.group(1).upper() in _VALID_STATE_CODES:
        return name[:m.start()].rstrip(" -–—,"), m.group(1).upper()

    return name, None


def normalize_state(raw: str | None) -> str | None:
    """Return a 2-letter code, or None. Never return an unrecognised string --
    an unknown value passed to the matcher suppresses the match entirely.

    The description regex is greedy and captures trailing prose, in either
    direction: a spelled-out name picks up trailing words ("Colorado with an
    eas") -- try the longest leading word-run first and shrink, which also
    keeps multi-word states ("New York", "North Carolina") intact. But a
    description that's ALREADY abbreviated ("...in Lakewood, CO with an easy
    to use...") captures "CO with an easy to u" -- the shrink loop only
    recognizes full names via STATE_CODES' keys, so it never tries the
    leading 2-letter token and silently drops a real match (Fox Hollow Golf,
    found 2026-08-19: this exact description, sitting unmatched for it).
    Check that first.
    """
    if not raw:
        return None
    s = raw.strip().strip(".,")
    if len(s) == 2 and s.isalpha():
        return s.upper()
    words = s.split()
    if words and len(words[0]) == 2 and words[0].isalpha() and words[0].upper() in _VALID_STATE_CODES:
        return words[0].upper()
    for n in range(len(words), 0, -1):
        code = STATE_CODES.get(" ".join(words[:n]).lower())
        if code:
            return code
    return None


def candidate_name(app: dict) -> tuple[str | None, str | None, str | None]:
    """Best (name, city, state) guess for the facility an app belongs to.

    Order matters. The two description patterns that also capture "in City, ST" are
    worth preferring because location disambiguates. A bare `welcome to X` capture is
    NOT better than the track_name -- it is a greedy regex over marketing prose, and
    preferring it is what produced "Dairy Creek Golf Course App".
    """
    desc = app.get("description") or ""

    # 1. Patterns carrying an explicit city/state.
    for pat in _DESC_PATTERNS_WITH_LOCATION:
        m = pat.search(desc)
        if m:
            g = m.groupdict()
            nm = clean_name(g.get("name"))
            if nm and len(nm) >= 4:
                return (nm, (g.get("city") or "").strip() or None,
                        (g.get("state") or "").strip() or None)

    # 2. The app's own title, cleaned. Usually the most reliable single field.
    nm = clean_name(app.get("track_name"))
    if nm and len(nm) >= 4:
        stripped, state = strip_trailing_state(nm)
        if state:
            stripped = clean_name(stripped)
            if stripped and len(stripped) >= 4:
                return (stripped, None, state)
        return (nm, None, None)

    # 3. Last resort: the loose prose pattern.
    for pat in _DESC_PATTERNS_LOOSE:
        m = pat.search(desc)
        if m:
            nm = clean_name(m.groupdict().get("name"))
            if nm and len(nm) >= 4:
                return (nm, None, None)

    return (None, None, None)


@dataclass
class JoinStats:
    apps_considered: int = 0
    requests_sent: int = 0
    matched: int = 0
    unmatched: int = 0
    facilities_with_app: int = 0
    facilities_without_app: int = 0
    by_status: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _submit(items: list[dict], threshold: float) -> str:
    r = requests.post(
        f"{config.MATCH_API_BASE}/match/facility/bulk",
        json={"confidence_threshold": threshold,
              "include_candidates": True,
              "requests": items},
        timeout=180,
    )
    r.raise_for_status()
    return r.json()["job_id"]


def _poll(job_id: str, total: int, timeout_s: int = 3600) -> list[dict]:
    """Wait for the job, then pull results.

    Status and results are separate endpoints on this service:
      GET .../bulk/{job_id}/status   -> {status, progress, statistics}
      GET .../bulk/{job_id}/results  -> {results: [...]}
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        r = requests.get(
            f"{config.MATCH_API_BASE}/match/facility/bulk/{job_id}/status",
            timeout=120)
        r.raise_for_status()
        data = r.json()
        status = data.get("status", "unknown")
        if status == "completed":
            break
        if status == "failed":
            raise RuntimeError(f"match job {job_id} failed: {data}")
        prog = data.get("progress", {})
        log.info("    %s %s/%s", status, prog.get("completed"), prog.get("total"))
        time.sleep(config.MATCH_POLL_INTERVAL_S)
    else:
        raise TimeoutError(f"match job {job_id} did not finish in {timeout_s}s")

    r = requests.get(
        f"{config.MATCH_API_BASE}/match/facility/bulk/{job_id}/results",
        params={"limit": total}, timeout=180)
    r.raise_for_status()
    return r.json().get("results", []) or []


def apply_crosswalk() -> set[int]:
    """app_crosswalk is the durable, human-curated answer for a track_id --
    manually confirmed facility/owner, or a permanent exclusion. Checked
    BEFORE match-service ever runs: a crosswalk row always wins over whatever
    the algorithmic pass would have guessed, and never gets re-litigated by a
    re-run the way a plain match_service link could.

    facility_ids is an ARRAY -- a crosswalk row is the ONLY sanctioned way
    for one app to legitimately map to more than one facility (Derek,
    2026-08-20). Every automated method (match_service, domain, subtitle,
    OCR, Lens) stays constrained to exactly one link per app, same as the
    collision-cleanup work all session; a real multi-course app only gets
    multiple ga_facility_app rows here, expanded from one manually-reviewed
    array, never from an algorithm guessing twice.

    Returns the set of track_ids the crosswalk has already settled -- these
    are pulled out of the match-service candidate pool entirely, whether
    resolved (facility/owner) or excluded.
    """
    rows = db.query("SELECT track_id, link_type, facility_ids, owner_id, "
                    "exclude_reason, source FROM app_crosswalk")
    facility_rows = [r for r in rows if r["link_type"] == "facility"]
    links = [{
        "facility_id": fid, "track_id": r["track_id"],
        "match_confidence": 1.0, "match_method": "manual_crosswalk",
        "match_status": f"app_crosswalk: {r['source']}",
    } for r in facility_rows for fid in (r["facility_ids"] or [])]

    # An owner-type row is a strong link to EVERY facility that owner
    # parents (Derek, 2026-08-20) -- not just an exclusion from the
    # candidate pool. Resolved fresh from facility.owner_id each run rather
    # than stored, so a facility later added/removed under that owner is
    # picked up automatically without re-editing the crosswalk row.
    owner_rows = [r for r in rows if r["link_type"] == "owner"]
    owner_link_count = 0
    if owner_rows:
        owner_ids = {r["owner_id"] for r in owner_rows}
        owned_facilities: dict[int, list[int]] = {}
        for f in db.query(f"SELECT facility_id, owner_id FROM facility "
                          f"WHERE owner_id IN ({','.join(str(o) for o in owner_ids)})"):
            owned_facilities.setdefault(f["owner_id"], []).append(f["facility_id"])
        for r in owner_rows:
            for fid in owned_facilities.get(r["owner_id"], []):
                links.append({
                    "facility_id": fid, "track_id": r["track_id"],
                    "match_confidence": 1.0, "match_method": "manual_crosswalk",
                    "match_status": f"app_crosswalk (owner {r['owner_id']}): {r['source']}",
                })
                owner_link_count += 1

    if links:
        db.upsert("ga_facility_app", links, on_conflict="facility_id,track_id")
    log.info("crosswalk: %d facility rows -> %d facility links (%.1f avg), "
            "%d owner rows -> %d facility links, %d excluded -- all pulled "
            "out of the match-service pool",
            len(facility_rows), len(links) - owner_link_count,
            (len(links) - owner_link_count) / len(facility_rows) if facility_rows else 0,
            len(owner_rows), owner_link_count,
            sum(1 for r in rows if r["link_type"] == "exclude"))
    return {r["track_id"] for r in rows}


def run(threshold: float = config.MATCH_CONFIDENCE_THRESHOLD,
        limit: int | None = None) -> JoinStats:
    stats = JoinStats()

    crosswalked = apply_crosswalk()

    apps = db.query("""
        SELECT a.track_id, a.track_name, a.description, a.bundle_id, v.vendor
        FROM ga_app a
        JOIN ga_app_vendor v ON v.track_id = a.track_id AND v.vendor IS NOT NULL
        WHERE a.delisted_at IS NULL
          AND a.not_a_golf_course_at IS NULL
          AND a.canadian_course_at IS NULL
        ORDER BY a.track_id
    """)
    apps = [a for a in apps if a["track_id"] not in crosswalked]
    if limit:
        apps = apps[:limit]
    stats.apps_considered = len(apps)

    # A state is REQUIRED before submitting (Derek, 2026-08-21): a bare name with
    # no state searches the entire in-scope facility pool nationally, not a real
    # match attempt -- measured, 90.2% of match_service links had no state filter
    # before this fix, with a real measured share colliding with a same-named
    # facility in a different state. No state -> skipped here, stays unmatched.
    items, index = [], []
    no_state_skipped = 0
    for a in apps:
        name, city, state = candidate_name(a)
        if not name or len(name) < 3:
            continue
        code = normalize_state(state)
        if not code:
            no_state_skipped += 1
            continue
        item = {"facility_name": name, "state": code}
        if city:
            item["city"] = city
        items.append(item)
        index.append(a["track_id"])

    log.info("join: %d apps -> %d match requests (%d skipped, no resolvable state)",
             len(apps), len(items), no_state_skipped)

    # The match service searches the WHOLE facility table -- all 21,250 rows including
    # Indoor Golf and Driving Ranges. It will happily return "The Golf Bar | Rancho
    # Bernardo" at confidence 1.0 for a country club's app. Accepting a facility we do
    # not even count in the denominator is worse than leaving the app unmatched, so
    # links are constrained to the configured scope and rejects go to review.
    in_scope = {r["facility_id"] for r in db.query(
        f"SELECT facility_id FROM facility WHERE {config.FACILITY_WHERE}")}
    log.info("join: constraining matches to %d in-scope facilities", len(in_scope))
    rejected_out_of_scope = 0

    links, ambiguous = [], []
    for i in range(0, len(items), config.MATCH_BATCH_SIZE):
        chunk = items[i:i + config.MATCH_BATCH_SIZE]
        ids = index[i:i + config.MATCH_BATCH_SIZE]
        job = _submit(chunk, threshold)
        log.info("  submitted batch %d (%d items) job=%s", i // config.MATCH_BATCH_SIZE,
                 len(chunk), job)
        results = _poll(job, total=len(chunk))
        stats.requests_sent += len(chunk)

        # Results come back in request order. Note `facility_name` in a result is the
        # MATCHED facility's name, not the name we submitted, so it cannot be used to
        # re-key the list -- position is the only correspondence available.
        if len(results) != len(chunk):
            raise RuntimeError(
                f"match job {job} returned {len(results)} results for {len(chunk)} "
                f"requests; positional mapping would misattribute every app")

        for track_id, res in zip(ids, results):
            status = res.get("matchStatus") or "unknown"
            stats.by_status[status] = stats.by_status.get(status, 0) + 1
            fid = res.get("facility_id")
            if fid and int(fid) not in in_scope:
                rejected_out_of_scope += 1
                stats.by_status[status + " (rejected: out of scope)"] = (
                    stats.by_status.get(status + " (rejected: out of scope)", 0) + 1)
                fid = None
            if fid:
                links.append({
                    "facility_id": int(fid),
                    "track_id": track_id,
                    "match_confidence": res.get("confidence"),
                    "match_method": "match_service",
                    "match_status": status,
                })
                stats.matched += 1
            else:
                stats.unmatched += 1
                cands = res.get("candidates") or []
                if cands or status == "Multiple Matches":
                    req = chunk[ids.index(track_id)] if track_id in ids else {}
                    ambiguous.append({
                        "track_id": track_id,
                        "submitted_name": req.get("facility_name"),
                        "submitted_city": req.get("city"),
                        "submitted_state": req.get("state"),
                        "match_status": status,
                        "candidates": cands[:10],
                        "candidate_count": len(cands),
                    })

    if rejected_out_of_scope:
        log.warning("rejected %d matches to out-of-scope facilities (indoor golf, "
                    "driving ranges, closed courses)", rejected_out_of_scope)
    if links:
        # Upsert on (facility_id, track_id) dedupes a REPEATED pair, but not a
        # CHANGED answer for the same app -- re-running the join after a matcher
        # improvement doesn't replace the old match_service link, it adds a second
        # one beside it. Found 2026-08-19: 20 apps carrying two match_service links
        # from two different join runs hours apart, half of them stale, none ever
        # flagged, because nothing about this was an error -- it looked like
        # legitimate multi-course support. Clear the prior match_service links for
        # every app in THIS batch before writing the fresh ones.
        touched = sorted({l["track_id"] for l in links})
        for i in range(0, len(touched), 400):
            chunk = ",".join(str(t) for t in touched[i:i + 400])
            db.exec_sql(f"DELETE FROM ga_facility_app WHERE match_method='match_service' "
                       f"AND track_id IN ({chunk})")
        db.upsert("ga_facility_app", links, on_conflict="facility_id,track_id")
    if ambiguous:
        db.upsert("ga_match_ambiguous", ambiguous, on_conflict="track_id")
        log.info("recorded %d ambiguous matches with candidates", len(ambiguous))

    _record_negatives(stats)
    return stats


def _record_negatives(stats: JoinStats) -> None:
    """Facilities with no app are data, not absence of data -- they are the
    penetration denominator and they let the monthly run skip re-searching."""
    rows = db.query(f"""
        SELECT count(*) n FROM facility f
        WHERE {config.FACILITY_WHERE}
          AND EXISTS (SELECT 1 FROM ga_facility_app fa WHERE fa.facility_id = f.facility_id)
    """)
    stats.facilities_with_app = rows[0]["n"] if rows else 0
    stats.facilities_without_app = db.facility_count() - stats.facilities_with_app

    db.exec_sql(f"""
        INSERT INTO ga_facility_no_app (facility_id, last_searched_at, search_count)
        SELECT f.facility_id, now(), 1
        FROM facility f
        WHERE {config.FACILITY_WHERE}
          AND NOT EXISTS (SELECT 1 FROM ga_facility_app fa
                          WHERE fa.facility_id = f.facility_id)
        ON CONFLICT (facility_id) DO UPDATE
          SET last_searched_at = now(),
              search_count = ga_facility_no_app.search_count + 1
    """)
