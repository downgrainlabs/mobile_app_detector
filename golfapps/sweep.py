"""Phase 1 -- build the golf-app corpus.

Corpus-first, not per-facility: 13,922 facilities x 3 name variants would be ~42k
queries of massively overlapping work. Sweeping generic + geographic + vendor terms
costs ~1,200 queries, and the residual search then only covers what fails to match.

Two things the original plan got wrong, both measured:
  * There is no template-phrase sweep. /search does not index descriptions at all
    (Check 2), so those ~50 queries would return couples' games and nothing else.
  * `offset` does not paginate and each query caps near 190 results (Check 3).

...and one thing I got wrong myself, worth stating plainly because the fix is not
obvious: hitting that ~190 ceiling does NOT mean relevant results are being hidden.
Apple pads any golf-ish query out to ~190 with loosely related apps, so 91% of
queries "look truncated". Splitting on that alone queued 9,024 sub-queries and decayed
to 0.36 new apps per query without converging. The ceiling is necessary but not
sufficient evidence of a hidden vein -- see MIN_NEW_APPS_TO_SPLIT.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import config, db, itunes, vendors

log = logging.getLogger(__name__)

GENERIC_TERMS = [
    "golf tee times", "book tee times", "tee sheet", "golf course app",
    "golf gps scorecard", "golf club app", "golf booking", "country club app",
    "member portal golf", "club member app", "golf course", "private club app",
    "golf reservations", "pro shop app", "golf club members",
]

STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois",
    "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland",
    "Massachusetts", "Michigan", "Minnesota", "Mississippi", "Missouri", "Montana",
    "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania",
    "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah",
    "Vermont", "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
]

GEO_PATTERNS = ["golf {}", "{} tee times", "{} golf club"]

# Max children a single truncated query may spawn. Bounds the blow-up from one
# over-broad term without silently capping coverage -- the shortfall is logged.
SPLIT_FANOUT = 20

# Hitting the ~190 ceiling does NOT mean relevant results are hidden.
#
# Measured 2026-08-18: 91% of golf queries return >=185 results, because Apple pads
# any golf-ish term with loosely related apps. 'golf spencerport ny' returns 191
# results consisting of GolfNow, Myrtle Beach, PGA TOUR and courses in other states --
# none of them in Spencerport. Splitting on the ceiling alone therefore fires on
# almost every query: a first run queued 9,024 splits and decayed to 0.36 new apps
# per query while never approaching termination.
#
# The ceiling is necessary but not sufficient. Split only when a query is also
# PRODUCTIVE -- i.e. it surfaced enough genuinely new apps to suggest there is a vein
# worth digging into.
MIN_NEW_APPS_TO_SPLIT = 8

# Hard ceiling on splits per run, so a pathological seed cannot run away again.
MAX_SPLITS_PER_RUN = 300


@dataclass
class SweepStats:
    queries_issued: int = 0
    queries_cached: int = 0
    queries_split: int = 0
    apps_found: int = 0
    new_apps: int = 0
    artist_expansions: int = 0
    truncated_queries: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "queries_issued": self.queries_issued,
            "queries_cached": self.queries_cached,
            "queries_split": self.queries_split,
            "apps_found": self.apps_found,
            "new_apps": self.new_apps,
            "artist_expansions": self.artist_expansions,
            "truncated_queries": self.truncated_queries[:50],
        }


_CORE_NAME_STOPWORDS = re.compile(
    r"\b(golf|club|country|course|links|resort|tennis|the|of|gc|cc|g|c|"
    r"and|at|members?)\b")
_CORE_NAME_JUNK = re.compile(r"[^a-z0-9 ]")


def core_name(facility_name: str) -> str:
    """Strip category/connector words down to the distinguishing part of a
    facility name, for name-driven App Store search (Derek, 2026-08-22/23).

    Searching this instead of the full name is validated to be as good or
    better, never worse -- e.g. 'Lake City' alone found 2 real apps that
    'Lake City Country Club' found ZERO of (the longer, more specific phrase
    diluted Apple's relevance ranking away from apps that don't literally say
    "Country Club"). Tested against 5 name clusters, core-name search never
    lost anything the full name found and twice found MORE.

    Two bugs fixed here after a 200-facility recall check (168/200 = 84% hit
    rate) surfaced them: (1) stopwords was too narrow -- "tennis"/"and"/"at"/
    "members" survived and diluted the query the same way appending
    city/state does (proven separately: adding tokens on top of an
    already-good core name only ever lost coverage, never gained it);
    (2) an all-stopword name like "The Country Club" stripped to an EMPTY
    string, and a naive fallback (strip only "the") left "country club" --
    just as generic and dilutive as no stripping at all. The real fallback is
    the full name: some facilities genuinely have no more specific term to
    search than their whole name.
    """
    low = _CORE_NAME_JUNK.sub(" ", facility_name.lower())
    low = re.sub(r"\s+", " ", low).strip()
    core = re.sub(r"\s+", " ", _CORE_NAME_STOPWORDS.sub(" ", low)).strip()
    return core or low


def metro_terms() -> list[str]:
    """Cities that actually have golf facilities, straight from the facility table.
    Beats a hardcoded metro list -- it targets exactly where the courses are."""
    rows = db.query(f"""
        SELECT city, state_code, count(*) n
        FROM facility
        WHERE {config.FACILITY_WHERE} AND city IS NOT NULL
        GROUP BY 1, 2
        HAVING count(*) >= 4
        ORDER BY n DESC
        LIMIT 400
    """)
    return [f"{r['city']} {r['state_code']}" for r in rows]


def build_query_plan(include_geo: bool = True) -> list[str]:
    reg = vendors.load()
    plan: list[str] = list(GENERIC_TERMS)

    # Vendor names -- cheap and high precision.
    for v in reg.vendors.values():
        plan.append(v.display_name)
        plan.extend(t for t in v.tokens if len(t) > 4)

    if include_geo:
        for s in STATES:
            plan.extend(p.format(s) for p in GEO_PATTERNS[:2])
        for m in metro_terms():
            plan.append(f"golf {m}")

    seen, out = set(), []
    for q in plan:
        n = itunes.normalize_query(q)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def split_query(term: str) -> list[str]:
    """Narrow a query that hit the ~190 ceiling.

    There is no pagination, so the only way to see past the ceiling is to ask a more
    specific question. Geography is the only axis that genuinely partitions results:
    a term already carrying a state narrows to that state's busiest cities, and a
    bare term narrows by state.

    Appending topic words instead ("golf booking" -> "golf booking tee times") does
    not partition anything -- it just produces overlapping near-duplicates, and
    re-splitting those yields gibberish like "golf booking tee times tee times".
    """
    low = itunes.normalize_query(term)

    # Already city-level? Nothing left to split on -- record the gap honestly.
    for s in STATES:
        if s.lower() in low:
            rows = db.query(f"""
                SELECT city
                FROM facility
                WHERE {config.FACILITY_WHERE}
                  AND state = {db.lit(s)} AND city IS NOT NULL
                GROUP BY 1 ORDER BY count(*) DESC LIMIT 25
            """)
            base = low.replace(s.lower(), "").strip()
            out = []
            for r in rows:
                city = (r["city"] or "").lower().strip()
                if city and city not in base:
                    out.append(itunes.normalize_query(f"{base} {city}"))
            return out

    # Bare term -> qualify by state, densest first. Capped so one truncated generic
    # term cannot spawn 50 children; the cap is logged, never silent.
    rows = db.query(f"""
        SELECT state, count(*) n FROM facility
        WHERE {config.FACILITY_WHERE} AND state IS NOT NULL
        GROUP BY 1 ORDER BY n DESC LIMIT {SPLIT_FANOUT}
    """)
    states = [r["state"] for r in rows]
    skipped = len(STATES) - len(states)
    if skipped > 0:
        log.info("split %r -> top %d states by facility count; %d lower-density "
                 "states not split out (already covered by the geographic sweep)",
                 term, len(states), skipped)
    return [itunes.normalize_query(f"{low} {s}") for s in states]


def run(limit: int | None = None, include_geo: bool = True,
        max_depth: int = 2) -> SweepStats:
    """Execute the sweep. Resumable: cached queries are skipped for free."""
    client = itunes.ITunesClient()
    stats = SweepStats()

    cached = db.cached_queries()
    known = db.known_track_ids()
    queue = [(q, 0) for q in build_query_plan(include_geo) if q not in cached]
    stats.queries_cached = len(cached)
    log.info("sweep: %d queries queued, %d already cached", len(queue), len(cached))

    seen_this_run: set[str] = set()

    while queue:
        # `limit` caps TOTAL queries issued, not just the seed queue -- a truncated
        # query spawns children, so capping only the seed makes --limit meaningless.
        if limit and stats.queries_issued >= limit:
            log.info("hit --limit %d (%d queries still queued)", limit, len(queue))
            break

        term, depth = queue.pop(0)
        if term in seen_this_run or term in cached:
            continue
        seen_this_run.add(term)

        results, truncated = client.search(term)
        stats.queries_issued += 1

        rows = [itunes.app_row(r) for r in results if r.get("trackId")]
        new_ids: set[int] = set()
        if rows:
            db.upsert("ga_app", rows, on_conflict="track_id")
            new_ids = {r["track_id"] for r in rows} - known
            known |= new_ids
            stats.new_apps += len(new_ids)
            stats.apps_found += len(rows)

        db.upsert("ga_search_cache", [{
            "query_normalized": term,
            "track_ids": [r["track_id"] for r in rows],
            "result_count": len(rows),
            "truncated": truncated,
        }], on_conflict="query_normalized")
        cached.add(term)

        # Split only a query that is BOTH at the ceiling and productive. The ceiling
        # alone is nearly universal and carries almost no information -- see the note
        # on MIN_NEW_APPS_TO_SPLIT.
        if truncated:
            stats.truncated_queries.append(term)
        productive = len(new_ids) >= MIN_NEW_APPS_TO_SPLIT
        if truncated and productive and depth < max_depth:
            if stats.queries_split >= MAX_SPLITS_PER_RUN:
                log.warning("split cap %d reached -- not splitting %r "
                            "(coverage gap recorded, not silent)",
                            MAX_SPLITS_PER_RUN, term)
            else:
                children = [c for c in split_query(term)
                            if c not in cached and c not in seen_this_run]
                if children:
                    db.upsert("ga_query_split",
                              [{"parent_query": term, "child_query": c}
                               for c in children],
                              on_conflict="parent_query,child_query")
                    queue.extend((c, depth + 1) for c in children)
                    stats.queries_split += 1
                    log.info("split %r (%d results, %d NEW) into %d narrower queries",
                             term, len(rows), len(new_ids), len(children))
        elif truncated and not productive:
            log.debug("%r hit the ceiling but yielded only %d new apps -- not "
                      "splitting (padding, not hidden results)", term, len(new_ids))

        if stats.queries_issued % 25 == 0:
            log.info("sweep progress: %d issued, %d apps (%d new), 429=%d 403=%d",
                     stats.queries_issued, stats.apps_found, stats.new_apps,
                     client.n_429, client.n_403)

    expand_artists(client, stats)
    return stats


def expand_artists(client: itunes.ITunesClient, stats: SweepStats,
                   max_accounts: int = 250) -> None:
    """Pull whole portfolios from developer accounts.

    Worth doing, but not the headline step the plan believed. It is high-yield only
    for vendor_account publishers (Chronogolf: 222 apps under one account) and weak
    for operator_account ones (Sagacity's own account holds 24 apps and only 11 of
    its 29 known courses). Run to fixpoint: new apps reveal new accounts.
    """
    seen: set[int] = set()
    for _ in range(3):  # fixpoint, bounded
        rows = db.query(f"""
            SELECT DISTINCT a.artist_id
            FROM ga_app a
            LEFT JOIN ga_app_vendor v ON v.track_id = a.track_id
            WHERE a.artist_id IS NOT NULL
              AND (v.vendor IS NOT NULL OR a.bundle_id ILIKE 'com.%')
            LIMIT {max_accounts}
        """)
        todo = [r["artist_id"] for r in rows if r["artist_id"] not in seen]
        if not todo:
            break
        log.info("expand_artists: %d accounts to expand this pass (~2s each, "
                 "unproxied -- expect ~%ds with no further output)",
                 len(todo), len(todo) * 2)
        for i, aid in enumerate(todo, 1):
            seen.add(aid)
            apps = client.artist_apps(aid)
            stats.artist_expansions += 1
            new_rows = [itunes.app_row(a) for a in apps if a.get("trackId")]
            if new_rows:
                db.upsert("ga_app", new_rows, on_conflict="track_id")
                stats.apps_found += len(new_rows)
            if i % 25 == 0:
                log.info("  expand_artists: %d/%d accounts done", i, len(todo))


@dataclass
class FacilityNameSweepStats:
    queries_issued: int = 0
    query_errors: int = 0
    new_apps_found: int = 0

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def facility_name_sweep(proxies: dict | None = None, workers: int = 20) -> FacilityNameSweepStats:
    """Discovery channel 4 (Derek, 2026-08-22/23): search directly by each
    UNLINKED facility's core (suffix-stripped) name, instead of relying on
    generic topic terms to surface it as padding.

    Validated against 5 real name clusters: core-name search never lost
    anything the full facility name found, and twice found things the full
    name missed entirely (e.g. "Lake City" found 2 real apps that "Lake City
    Country Club" found ZERO of -- the longer phrase diluted Apple's
    relevance ranking away from apps that don't literally say "Country
    Club"). Adding city/state on TOP of the core name was also tested and
    makes things WORSE, never better -- same dilution effect. So: core name
    alone, nothing appended.

    Recall-checked against 200 already-linked facilities (any method): 84%
    hit rate. The 16% miss rate is NOT random -- it clusters into real,
    understood categories (owner/municipal-branded apps that never mention
    the facility name at all; resort/development-wide branding; overly
    generic single-word names crowded out by the ~190-result ceiling), not
    evidence this channel is unreliable. See docs/ from 2026-08-23 for the
    full breakdown.

    Direct-IP query volume this size (thousands of novel queries) degrades
    even normally-open Apple endpoints -- pass `proxies` (config.proxy_config())
    to route through Evomi instead. Falls back to the standing single-worker
    itunes.ITunesClient pacing if proxies is None, just slower.

    This is a DISCOVERY step only -- it adds new vendor-confirmed apps to
    ga_app/ga_app_vendor and returns. It deliberately does NOT attempt to
    match them to a facility itself (that was a real bug, fixed 2026-08-23:
    an earlier one-off version of this skipped lens.likely_non_us() entirely,
    so ~55 Canadian/UK/Irish Chronogolf apps went straight into the matching
    waterfall unfiltered instead of getting caught as non-US). Every app this
    finds MUST flow through rebuild.run() afterward -- that is the single
    place the full waterfall (crosswalk -> non-US filter -> domain -> OCR ->
    crawl -> domain -> subtitle -> name+state match) lives. Never
    reimplement any part of that waterfall in a one-off script again.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    stats = FacilityNameSweepStats()
    reg = vendors.load()
    known_ids = db.known_track_ids()

    rows = db.query(f"""
        SELECT DISTINCT facility_name FROM facility f
        LEFT JOIN ga_facility_app fa ON fa.facility_id = f.facility_id
        WHERE {config.FACILITY_WHERE} AND fa.facility_id IS NULL
    """)
    terms = sorted({core_name(r["facility_name"]) for r in rows
                    if len(core_name(r["facility_name"])) > 2})
    log.info("facility_name_sweep: %d distinct core-name queries", len(terms))

    client = itunes.ITunesClient(min_interval=0.0 if proxies else config.API_MIN_INTERVAL_S,
                                 proxies=proxies, pool_size=workers + 10)
    found: dict[int, dict] = {}

    def do_search(term: str):
        try:
            results, _truncated = client.search(term)
            return results
        except Exception as e:
            log.warning("facility_name_sweep: query failed for %r: %s", term, e)
            return None

    # Submitted in bounded batches, not all len(terms) futures at once (up to
    # 8,600+) -- with a proxy that's flaky under load, worker threads can end
    # up parked in retry backoff (itunes.ITunesClient._get()'s up-to-4-retry
    # schedule) while thousands of already-submitted results pile up faster
    # than the main loop drains them. Found 2026-09-11: a Render cron job on a
    # 512Mi plan OOM'd right as this step started, right after the base sweep
    # phase had already churned through 26k+ API results. Chunking keeps the
    # in-flight backlog bounded to roughly `workers` regardless of how many
    # terms there are, and lets each batch's memory actually get reclaimed
    # before the next one starts.
    done = 0
    SUBMIT_CHUNK = workers * 10
    with ThreadPoolExecutor(max_workers=workers if proxies else 1) as ex:
        for i in range(0, len(terms), SUBMIT_CHUNK):
            chunk = terms[i:i + SUBMIT_CHUNK]
            futures = {ex.submit(do_search, t): t for t in chunk}
            for fut in as_completed(futures):
                results = fut.result()
                done += 1
                stats.queries_issued += 1
                if results is None:
                    stats.query_errors += 1
                else:
                    for x in results:
                        tid = x.get("trackId")
                        if not tid or tid in known_ids:
                            continue
                        row = itunes.app_row(x)
                        lab = vendors.label_app(row, reg)
                        if lab.confidence in ("high", "medium") and tid not in found:
                            found[tid] = {"row": row, "vendor": lab.vendor,
                                          "confidence": lab.confidence}
                if done % 500 == 0:
                    log.info("  facility_name_sweep: %d/%d done, %d new apps found",
                             done, len(terms), len(found))

    if found:
        app_rows, vendor_rows = [], []
        for tid, d in found.items():
            row = dict(d["row"])
            row["track_id"] = tid
            app_rows.append(row)
            vendor_rows.append({"track_id": tid, "vendor": d["vendor"],
                               "confidence": d["confidence"]})
        for i in range(0, len(app_rows), 500):
            db.upsert("ga_app", app_rows[i:i + 500], on_conflict="track_id")
        for i in range(0, len(vendor_rows), 500):
            db.upsert("ga_app_vendor", vendor_rows[i:i + 500], on_conflict="track_id")

    stats.new_apps_found = len(found)
    log.info("facility_name_sweep done: %s", stats.as_dict())
    return stats
