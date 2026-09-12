"""Stateless full-recompute pipeline (post-sweep, pre-Lens).

app_crosswalk is the only durable answer this trusts between runs. Everything
else -- domain, OCR, subtitle, name/description match-service -- is
recomputed fresh every call from CURRENT ga_app + facility + app_crosswalk,
tried as a waterfall (first technique to land an in-scope facility wins, no
cross-checking between techniques), and the result REPLACES ga_facility_app
for every non-crosswalk row rather than accumulating on top of whatever an
earlier run happened to write.

Order (Derek, 2026-08-21): domain -> OCR (ClubCaddie/Tenfore only) ->
subtitle -> name/description match-service. No Lens step -- Lens output is
never trustworthy on its own; it only becomes real once a human reviews it
and it lands in app_crosswalk.

Crawl is LAZY, not upfront (Derek, 2026-08-21). The free iTunes JSON API only
ever gives seller_url -- support_url, privacy_policy_url, developer_website,
and subtitle all live ONLY in the scraped HTML product page, one request per
app, no batching, and Apple rate-limits it hard. Fetching that for every
candidate before trying anything else (the first version of this script)
means paying the slow, unbatched, rate-limited cost for apps that domain
match or OCR would have already resolved for nothing.

IMPORTANT, corrected 2026-08-21 (Derek caught this): domain-match "pass 1"
below is NOT "free-JSON-fields-only" -- _domain_match_pass() always reads
whatever is CURRENTLY stored in ga_app across all four URL fields, and most
of the corpus already has support_url/privacy_policy_url/developer_website
populated from HTML crawls done earlier this session (3,229 apps, measured).
So pass 1 already benefits from months of accumulated HTML data; it is only
"free" in the sense that it costs nothing to READ what is already stored.
Pass 2 only has a chance to matter for the handful of apps that had NEVER
been crawled before and get crawled fresh in step 7 -- on a corpus this
heavily pre-crawled, pass 2 will usually find little to nothing new. The
crawl is genuinely lazy (scoped to the leftover pool, not everyone) but it is
not the primary source of domain-match signal -- the pre-existing crawl
history is.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import (config, db, domainmatch, enrich, join, lens, report,
               review_queue, screenshot_ocr, subtitle_resolve, sweep)

log = logging.getLogger(__name__)


@dataclass
class RebuildStats:
    discovery_queries: int = 0
    discovery_new_apps: int = 0
    liveness_checked: int = 0
    liveness_delisted: int = 0
    pool_size: int = 0
    excluded_non_us: int = 0
    resolved_domain_pass1: int = 0
    resolved_ocr: int = 0
    html_crawled: int = 0
    resolved_domain_pass2: int = 0
    resolved_subtitle: int = 0
    resolved_name_match: int = 0
    crosswalk_facility: int = 0
    crosswalk_owner: int = 0
    crosswalk_excluded: int = 0
    still_unmatched: int = 0
    unmatched_sample: list = field(default_factory=list)
    review_queue_new: int = 0
    review_queue_pending: int = 0
    review_queue_resolved: int = 0
    review_queue_delisted: int = 0

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["unmatched_sample"] = d["unmatched_sample"][:20]
        return d


def _pool() -> list[dict]:
    """Vendor-confirmed, live, not crosswalked. NOT filtered by any stored
    ga_app exclusion flag (not_a_golf_course_at / canadian_course_at) --
    those predate the crosswalk table and duplicate what crosswalk `exclude`
    rows now do; excluded here entirely via live app_crosswalk membership."""
    crosswalked = join.apply_crosswalk()
    rows = db.query("""
        SELECT a.track_id, a.track_name, a.description, a.subtitle,
               a.bundle_id, a.seller_name,
               a.seller_url, a.support_url, a.privacy_policy_url,
               a.developer_website, v.vendor, v.confidence
        FROM ga_app a
        JOIN ga_app_vendor v ON v.track_id = a.track_id AND v.vendor IS NOT NULL
        WHERE a.delisted_at IS NULL
        ORDER BY a.track_id
    """)
    return [r for r in rows if r["track_id"] not in crosswalked]


def _domain_match_pass(track_ids: list[int], in_scope: set[int]) -> dict[int, dict]:
    """Fresh per call -- re-reads ga_app, so a second pass after a crawl sees
    the newly-fetched fields. Returns {track_id: link_dict} for hits only."""
    if not track_ids:
        return {}
    id_set = set(track_ids)
    app_hosts = {tid: hs for tid, hs in domainmatch.app_hosts().items() if tid in id_set}
    fac_hosts = domainmatch.facility_hosts()
    out = {}
    for tid, hosts in app_hosts.items():
        hits = []
        for h in hosts:
            hits.extend(fac_hosts.get(h, []))
            reg = domainmatch.registrable(h)
            if reg and reg != h:
                hits.extend(fac_hosts.get(reg, []))
        unique = {f["facility_id"]: f for f in hits}
        if len(unique) != 1:
            continue
        fac = next(iter(unique.values()))
        if fac["facility_id"] not in in_scope:
            continue
        out[tid] = {
            "facility_id": fac["facility_id"], "track_id": tid,
            "match_confidence": 0.95, "match_method": "domain",
            "match_status": "Match - Website Domain",
        }
    return out


def run(apply_links: bool = False, run_discovery: bool = False,
        use_proxy: bool = False, run_id: str | None = None) -> RebuildStats:
    """The single, canonical pipeline. Every discovery channel (old
    generic/geo/vendor sweep, the newer facility-name sweep) must feed new
    apps into ga_app/ga_app_vendor and then let THIS function do the
    matching -- never reimplement any part of the waterfall in a one-off
    script. That exact mistake happened 2026-08-23: an ad-hoc script handling
    a batch of newly-discovered apps skipped the non-US sanity filter (step
    4 below), so ~55 Canadian/UK/Irish Chronogolf apps went straight into
    match_service unfiltered instead of getting caught, and sat in a review
    queue looking like unresolved mysteries when the system already knew
    exactly what they were.

    run_discovery=True runs sweep.facility_name_sweep() first (step 0) --
    the core-name-per-unlinked-facility search channel, validated 2026-08-23
    to find real apps the topic-term sweep misses. use_proxy=True routes
    both that and the HTML crawl step through config.proxy_config() (Evomi)
    -- required for run_discovery at any real scale (thousands of novel
    queries), and worth it for the crawl step too since a direct IP that's
    done heavy query volume elsewhere in the same session can get blocked on
    endpoints that are normally wide open (measured 2026-08-23).

    run_id: tags whatever's left in the review queue after the waterfall
    (review_queue.sync(), step 11 below) so a caller can tell "new this run"
    apart from backlog. NOT bucketed to any calendar period (Derek,
    2026-09-12: the code shouldn't assume monthly cadence just because
    that's what Render happens to be scheduled for today) -- defaults to
    whatever the current latest snapshot run_id is (refining it, same as a
    review-queue Sync would), or a fresh one (report.new_run_id()) if no
    snapshot exists yet. A bare `rebuild.run()` call (the `pipeline` CLI
    command, ad hoc local runs) still tags something sane without the
    caller having to think about it; `cli.py`'s `run-all` always passes an
    explicit fresh run_id instead, since a full scheduled execution is its
    own new sample point, not a refinement of the last one.
    """
    stats = RebuildStats()
    run_id = run_id or report.latest_run_id() or report.new_run_id()
    proxies = config.proxy_config() if use_proxy else None
    if use_proxy and not proxies:
        log.warning("use_proxy=True but EVOMI_* not configured in .env -- "
                    "falling back to direct connection")

    # ---- 0. discovery (optional) -- core-name search per unlinked facility -----
    if run_discovery:
        d_stats = sweep.facility_name_sweep(proxies=proxies)
        stats.discovery_queries = d_stats.queries_issued
        stats.discovery_new_apps = d_stats.new_apps_found
        log.info("rebuild: discovery %s", d_stats.as_dict())

    # ---- 1. liveness (fast, batched JSON API) ----------------------------------
    live = enrich.refresh_known_apps()
    stats.liveness_checked = live["checked"]
    stats.liveness_delisted = live["delisted"]
    log.info("rebuild: liveness %s", live)

    # ---- 2. vendor label from free JSON, escalate ONLY where inconclusive -----
    enrich.run(escalate=True, html_for_matching=False, only_course_apps=True)

    # ---- 3. crosswalk (always wins, resolved fresh) -----------------------------
    cw_rows = db.query("SELECT link_type, count(*) c FROM app_crosswalk GROUP BY link_type")
    for r in cw_rows:
        if r["link_type"] == "facility":
            stats.crosswalk_facility = r["c"]
        elif r["link_type"] == "owner":
            stats.crosswalk_owner = r["c"]
        elif r["link_type"] == "exclude":
            stats.crosswalk_excluded = r["c"]

    pool = _pool()
    stats.pool_size = len(pool)
    log.info("rebuild: %d apps in the waterfall pool (vendor-confirmed, live, "
             "not crosswalked)", len(pool))

    # ---- 4. sanity filter (non-US only) ----------------------------------------
    # lacks_golf_signal() dropped 2026-08-21 (Derek): tested by running the 489
    # apps it was excluding through the real waterfall with the filter bypassed
    # -- 480/489 (98.2%) resolved to a real, in-scope facility anyway, mostly via
    # domain match. The filter was excluding almost everything it touched; the
    # matching techniques themselves already guard against false positives.
    remaining = []
    for a in pool:
        if lens.likely_non_us(a):
            stats.excluded_non_us += 1
            continue
        remaining.append(a)
    log.info("rebuild: %d pass non-US sanity filter", len(remaining))

    by_id = {a["track_id"]: a for a in remaining}
    unresolved = dict(by_id)
    fresh_links: list[dict] = []

    in_scope = {r["facility_id"] for r in db.query(
        f"SELECT facility_id FROM facility WHERE {config.FACILITY_WHERE}")}

    # ---- 5. domain match, pass 1 -- whatever's ALREADY in ga_app across all 4
    # URL fields, no fresh crawl spent. Most of the corpus already has
    # support_url/privacy_policy_url/developer_website from crawls done
    # earlier this session, so this is doing most of the real work, not just
    # the bare seller_url the free JSON lookup provides.
    hits = _domain_match_pass(list(unresolved), in_scope)
    for tid, link in hits.items():
        fresh_links.append(link)
        stats.resolved_domain_pass1 += 1
        del unresolved[tid]
    log.info("rebuild: domain pass 1 (pre-existing data, any of the 4 fields) "
             "resolved %d, %d remain", stats.resolved_domain_pass1, len(unresolved))

    # ---- 6. screenshot OCR (ClubCaddie/Tenfore, screenshot is free-API too) ----
    ocr_ids = [tid for tid in unresolved if by_id[tid]["vendor"] in screenshot_ocr.SAFE_VENDORS]
    if ocr_ids:
        ocr_result = screenshot_ocr.run(ocr_ids)
        for r in ocr_result["resolved"]:
            if r["track_id"] not in unresolved:
                continue
            fresh_links.append(r)
            stats.resolved_ocr += 1
            del unresolved[r["track_id"]]
    log.info("rebuild: OCR resolved %d, %d remain", stats.resolved_ocr, len(unresolved))

    # ---- 7. crawl HTML -- ONLY for what's left, this is the slow/rate-limited step
    crawl_ids = list(unresolved)
    if crawl_ids:
        n = subtitle_resolve.fetch_missing(crawl_ids, proxies=proxies)
        stats.html_crawled = n
        log.info("rebuild: crawled HTML for %d of %d remaining apps", n, len(crawl_ids))

    # ---- 8. domain match, pass 2 -- only the apps crawled fresh in step 7 have
    # any NEW data since pass 1; on a heavily pre-crawled corpus this is usually
    # small (measured: 10 crawled -> 0 new hits, 2026-08-21).
    hits = _domain_match_pass(list(unresolved), in_scope)
    for tid, link in hits.items():
        fresh_links.append(link)
        stats.resolved_domain_pass2 += 1
        del unresolved[tid]
    log.info("rebuild: domain pass 2 (post-crawl) resolved %d, %d remain",
             stats.resolved_domain_pass2, len(unresolved))

    # ---- 9. subtitle / subheader text -------------------------------------------
    sub_ids = list(unresolved)
    if sub_ids:
        sub_result = subtitle_resolve.run(sub_ids)
        for r in sub_result["resolved"]:
            if r["track_id"] not in unresolved:
                continue
            fresh_links.append(r)
            stats.resolved_subtitle += 1
            del unresolved[r["track_id"]]
    log.info("rebuild: subtitle resolved %d, %d remain", stats.resolved_subtitle, len(unresolved))

    # ---- 10. name / description -> match-service (last resort, free JSON) ------
    # A state is REQUIRED before this is even attempted (Derek, 2026-08-21): a bare
    # name with no state searches the entire ~13,922-facility national pool, not a
    # real match attempt. Measured: 90.2% of match_service links (1,293 of 1,433)
    # had been submitted with no state filter at all before this fix, and a
    # measurable share of those collided with a same-named facility in a
    # DIFFERENT state. No state -> the app stays unmatched and goes to manual
    # review, it does not get a name-only guess against the whole country.
    name_items, name_index = [], []
    for tid, a in unresolved.items():
        name, city, state = join.candidate_name(a)
        if not name or len(name) < 3:
            continue
        code = join.normalize_state(state)
        if not code:
            continue
        item = {"facility_name": name, "state": code}
        if city:
            item["city"] = city
        name_items.append(item)
        name_index.append(tid)

    if name_items:
        for i in range(0, len(name_items), config.MATCH_BATCH_SIZE):
            chunk = name_items[i:i + config.MATCH_BATCH_SIZE]
            ids = name_index[i:i + config.MATCH_BATCH_SIZE]
            job = join._submit(chunk, config.MATCH_CONFIDENCE_THRESHOLD)
            results = join._poll(job, total=len(chunk))
            for tid, res in zip(ids, results):
                fid = res.get("facility_id")
                if fid and int(fid) in in_scope:
                    fresh_links.append({
                        "facility_id": int(fid), "track_id": tid,
                        "match_confidence": res.get("confidence"),
                        "match_method": "match_service",
                        "match_status": res.get("matchStatus"),
                    })
                    stats.resolved_name_match += 1
                    del unresolved[tid]
    log.info("rebuild: name/description match resolved %d, %d remain",
             stats.resolved_name_match, len(unresolved))

    stats.still_unmatched = len(unresolved)
    stats.unmatched_sample = [
        {"track_id": tid, "app_name": a["track_name"], "vendor": a["vendor"]}
        for tid, a in unresolved.items()
    ]

    # ---- 10b. sync the Retool review queue with whatever's still unresolved ----
    rq = review_queue.sync(unresolved, run_id)
    stats.review_queue_new = rq["new"]
    stats.review_queue_pending = rq["pending"]
    stats.review_queue_resolved = rq["resolved"]
    stats.review_queue_delisted = rq["delisted"]
    log.info("rebuild: review queue %s", rq)

    # ---- 11. commit: REPLACE non-crosswalk ga_facility_app, don't accumulate ---
    if apply_links:
        db.exec_sql("DELETE FROM ga_facility_app WHERE match_method != 'manual_crosswalk'")
        if fresh_links:
            db.upsert("ga_facility_app", fresh_links, on_conflict="facility_id,track_id")
        log.info("rebuild: wrote %d fresh links, replacing every non-crosswalk row",
                 len(fresh_links))
    else:
        log.info("rebuild: dry run, %d fresh links computed but NOT written",
                 len(fresh_links))

    return stats
