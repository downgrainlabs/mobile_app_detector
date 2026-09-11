"""Channel: App Store Connect's 'subtitle' field as a location signal.

A generic Apple field any developer can set -- not vendor-specific, unlike
screenshot OCR. Confirmed reliable for Chronogolf (2026-08-19: "River Bend
Golf Club" -> "Red Deer, Alberta"; "Fox Hollow" -> "Lakewood, CO"), but the
mechanism itself has no vendor dependency, so it's safe to attempt broadly --
a missing or unparseable subtitle is just a harmless no-op, not a false-
positive risk the way a shared demo screenshot is for OCR.
"""
from __future__ import annotations

import logging

from . import appstore, config, db

log = logging.getLogger(__name__)


def fetch_missing(track_ids: list[int], proxies: dict | None = None,
                  workers: int = 20) -> int:
    """HTML-fetch (free, no API cost) any app in track_ids with no subtitle
    and no prior HTML fetch. Returns count fetched. Apps already fetched
    (html_fetched_at set) are skipped even if subtitle came back None --
    re-fetching won't produce a different answer.

    Pass `proxies` (config.proxy_config()) to route through Evomi and crawl
    concurrently. This endpoint is normally wide open on a direct connection
    (316 rpm, 0 blocks measured), but a direct IP that has done heavy novel
    -query volume elsewhere the same session can get blocked here too --
    found 2026-08-23, a large HTML crawl stalled at ~0% success right after
    a big proxied search-API sweep, even though the two are different Apple
    endpoints. Falls back to the standing single-worker direct connection if
    proxies is None.
    """
    if not track_ids:
        return 0
    rows = db.query(f"""
        SELECT track_id FROM ga_app
        WHERE track_id IN ({','.join(str(t) for t in track_ids)})
          AND subtitle IS NULL AND html_fetched_at IS NULL
    """)
    to_fetch = [r["track_id"] for r in rows]
    if not to_fetch:
        return 0
    log.info("subtitle: fetching HTML for %d apps%s", len(to_fetch),
             " via proxy" if proxies else "")
    client = appstore.AppStoreClient(
        min_interval=0.0 if proxies else config.HTML_MIN_INTERVAL_S,
        proxies=proxies, pool_size=workers + 10)
    # PostgREST batch upserts require every row in the SAME call to share the
    # same keys ("All object keys must match") -- a delisted row (just
    # track_id + delisted_at) mixed into the same batch as normal fetch rows
    # (7 keys) gets rejected outright, failing the whole batch.
    updates, delisted = [], []

    def do_fetch(tid):
        return tid, client.fetch(tid)

    if proxies:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(do_fetch, tid) for tid in to_fetch]
            for i, fut in enumerate(as_completed(futures), 1):
                tid, page = fut.result()
                if page and page != "__DELISTED__":
                    f = appstore.extract(page)
                    updates.append({
                        "track_id": tid, "subtitle": f.get("subtitle"),
                        "copyright": f.get("copyright"),
                        "privacy_policy_url": f.get("privacy_policy_url"),
                        "support_url": f.get("support_url"),
                        "developer_website": f.get("developer_website"),
                        "html_fetched_at": appstore._utcnow(),
                    })
                elif page == "__DELISTED__":
                    delisted.append({"track_id": tid, "delisted_at": appstore._utcnow()})
                if len(updates) >= 300:
                    db.upsert("ga_app", updates, on_conflict="track_id")
                    updates = []
                if len(delisted) >= 100:
                    db.upsert("ga_app", delisted, on_conflict="track_id")
                    delisted = []
                if i % 200 == 0:
                    log.info("  subtitle fetch %d/%d", i, len(to_fetch))
    else:
        for i, tid in enumerate(to_fetch, 1):
            page = client.fetch(tid)
            if page and page != "__DELISTED__":
                f = appstore.extract(page)
                updates.append({
                    "track_id": tid, "subtitle": f.get("subtitle"),
                    "copyright": f.get("copyright"),
                    "privacy_policy_url": f.get("privacy_policy_url"),
                    "support_url": f.get("support_url"),
                    "developer_website": f.get("developer_website"),
                    "html_fetched_at": appstore._utcnow(),
                })
            elif page == "__DELISTED__":
                delisted.append({"track_id": tid, "delisted_at": appstore._utcnow()})
            if i % 100 == 0:
                if updates:
                    db.upsert("ga_app", updates, on_conflict="track_id")
                    updates = []
                if delisted:
                    db.upsert("ga_app", delisted, on_conflict="track_id")
                    delisted = []
                log.info("  subtitle fetch %d/%d", i, len(to_fetch))
    if updates:
        db.upsert("ga_app", updates, on_conflict="track_id")
    if delisted:
        db.upsert("ga_app", delisted, on_conflict="track_id")
    return len(to_fetch)


def run(track_ids: list[int]) -> dict:
    """Standard pre-Lens subtitle pass: fetch any missing subtitles, then try
    (name, state) from whatever subtitle text is on hand through match-
    service. Returns {"resolved": [...], "considered": n}. Does not write
    anything; callers decide whether/how to apply results.

    Pre-filters through lens.classify() BEFORE fetching -- no point spending
    an HTML fetch on an app that's already excluded (no vendor signature, no
    golf/tee-time signal). The non-US check runs a second time AFTER
    fetching, since it can also read the subtitle text just retrieved.
    """
    from . import join, config, lens as _lens

    pre = db.query(f"""
        SELECT a.track_id, a.track_name, a.description, a.subtitle, v.vendor
        FROM ga_app a LEFT JOIN ga_app_vendor v ON v.track_id = a.track_id
        WHERE a.track_id IN ({','.join(str(t) for t in track_ids)})
    """)
    eligible_ids = [r["track_id"] for r in pre if not _lens.classify(r, r["vendor"])]
    log.info("subtitle: %d of %d pass classify() pre-filter", len(eligible_ids), len(track_ids))

    fetch_missing(eligible_ids)

    rows = db.query(f"""
        SELECT track_id, track_name, description, subtitle, seller_url, support_url,
               privacy_policy_url, developer_website FROM ga_app
        WHERE track_id IN ({','.join(str(t) for t in eligible_ids)})
          AND subtitle IS NOT NULL
    """)
    rows = [r for r in rows if not _lens.likely_non_us(r)]
    log.info("subtitle: %d of %d eligible candidates have a usable (US) subtitle",
             len(rows), len(eligible_ids))

    items, index = [], []
    for r in rows:
        city, state = join.strip_trailing_state(r["subtitle"])
        if not state:
            continue
        name = join.clean_name(r["track_name"])
        if not name or len(name) < 3:
            continue
        item = {"facility_name": name, "state": state}
        if city:
            item["city"] = city
        items.append(item)
        index.append(r["track_id"])

    if not items:
        return {"resolved": [], "considered": len(rows)}

    in_scope = {r["facility_id"] for r in db.query(
        f"SELECT facility_id FROM facility WHERE {config.FACILITY_WHERE}")}
    resolved = []
    for i in range(0, len(items), config.MATCH_BATCH_SIZE):
        chunk = items[i:i + config.MATCH_BATCH_SIZE]
        ids = index[i:i + config.MATCH_BATCH_SIZE]
        job = join._submit(chunk, config.MATCH_CONFIDENCE_THRESHOLD)
        results = join._poll(job, total=len(chunk))
        for tid, res in zip(ids, results):
            fid = res.get("facility_id")
            if fid and int(fid) in in_scope:
                resolved.append({"track_id": tid, "facility_id": int(fid),
                                 "match_confidence": res.get("confidence"),
                                 "match_status": res.get("matchStatus"),
                                 "match_method": "subtitle_state_match"})
    log.info("subtitle: %d/%d resolved", len(resolved), len(rows))
    return {"resolved": resolved, "considered": len(rows)}
