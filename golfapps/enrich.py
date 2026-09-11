"""Phase 3 -- vendor labelling, with HTML fetch as an ESCALATION not a default.

The plan fetched HTML for every candidate app on the premise that no single field
reliably names the vendor. Measurement says otherwise: bundle_id + seller_url +
seller_name label roughly 40-50% of course booking apps straight from the free JSON
that Phase 1 already collected (docs/findings.md, Probe B).

So: label from the free JSON first, and spend an HTML fetch only on apps that come
back `low` or `unknown`. That is a large reduction in the most expensive phase, and
the apps that do get escalated are exactly the ones where copyright and the privacy
policy domain settle the answer.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from . import appstore, config, db, vendors

log = logging.getLogger(__name__)


@dataclass
class EnrichStats:
    labelled_from_json: int = 0
    escalated_to_html: int = 0
    resolved_by_html: int = 0
    still_unknown: int = 0
    delisted: int = 0
    conflicts: int = 0
    by_vendor: dict = field(default_factory=dict)
    by_confidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


# seller_url and developer_website survive a vendor migration unchanged -- Los
# Serranos still advertises quick18.com years after moving to Gallus. A label resting
# only on those is the single highest-value thing to escalate: it looks settled at
# `medium` and is exactly the case HTML resolves.
STALE_PRONE_FIELDS = {"seller_url", "developer_website"}


def _needs_html(lab: vendors.Label) -> bool:
    if lab.confidence in ("low", "unknown"):
        return True
    fields = {m["field"] for m in lab.matched_fields}
    return bool(fields) and fields <= STALE_PRONE_FIELDS


def _all_app_rows() -> list[dict]:
    return db.query("""
        SELECT track_id, bundle_id, track_name, seller_name, artist_id, artist_name,
               seller_url, description, copyright, privacy_policy_url, support_url,
               developer_website, html_fetched_at
        FROM ga_app
        WHERE delisted_at IS NULL
        ORDER BY track_id
    """)


def run(escalate: bool = True, max_html: int | None = None,
        only_course_apps: bool = True, html_for_matching: bool = False) -> EnrichStats:
    """
    html_for_matching: fetch HTML for EVERY course-app candidate, not just ones the
    free JSON could not label. The page carries support_url and privacy_policy_url,
    which are matching signals for domainmatch as well as labelling signals -- an app
    whose vendor is already known may still need its page fetched to find the club's
    own website. HTML is cheap (measured 316 req/min, zero blocks), so this is worth
    doing whenever the domain channel is going to run.
    """
    reg = vendors.load()
    stats = EnrichStats()
    rows = _all_app_rows()
    log.info("enrich: %d apps in corpus", len(rows))

    labels: dict[int, vendors.Label] = {}
    escalation_queue: list[dict] = []

    # --- pass 1: label from the free JSON ------------------------------------
    for r in rows:
        lab = vendors.label_app(r, reg)
        labels[r["track_id"]] = lab
        if lab.confidence in ("high", "medium"):
            stats.labelled_from_json += 1
        if r.get("html_fetched_at") is not None:
            continue
        if not (html_for_matching or _needs_html(lab)):
            continue
        # Only spend a fetch on something that plausibly is a course app.
        if not only_course_apps or vendors.looks_like_course_app(r):
            escalation_queue.append(r)

    log.info("enrich: %d labelled from free JSON, %d queued for HTML",
             stats.labelled_from_json, len(escalation_queue))

    # --- pass 2: escalate the residue ----------------------------------------
    if escalate and escalation_queue:
        if max_html:
            escalation_queue = escalation_queue[:max_html]
        client = appstore.AppStoreClient()
        updates, delisted = [], []
        for i, r in enumerate(escalation_queue, 1):
            page = client.fetch(r["track_id"])
            stats.escalated_to_html += 1
            if page is None:
                continue
            if page == "__DELISTED__":
                delisted.append({"track_id": r["track_id"],
                                 "delisted_at": appstore._utcnow()})
                stats.delisted += 1
                continue

            upd = appstore.enrich_row(r["track_id"], page)
            updates.append(upd)

            merged = dict(r) | {k: v for k, v in upd.items() if k != "track_id"}
            relabelled = vendors.label_app(merged, reg)
            if relabelled.confidence in ("high", "medium"):
                stats.resolved_by_html += 1
            labels[r["track_id"]] = relabelled

            if i % 100 == 0:
                log.info("  html %d/%d (resolved %d)", i, len(escalation_queue),
                         stats.resolved_by_html)
                db.upsert("ga_app", updates, on_conflict="track_id")
                updates = []

        if updates:
            db.upsert("ga_app", updates, on_conflict="track_id")
        if delisted:
            db.upsert("ga_app", delisted, on_conflict="track_id")

    # --- persist -------------------------------------------------------------
    out = []
    for tid, lab in labels.items():
        out.append(lab.as_row(tid))
        stats.by_confidence[lab.confidence] = stats.by_confidence.get(lab.confidence, 0) + 1
        if lab.vendor:
            stats.by_vendor[lab.vendor] = stats.by_vendor.get(lab.vendor, 0) + 1
        if lab.conflict:
            stats.conflicts += 1
        if lab.confidence == "unknown":
            stats.still_unknown += 1
    db.upsert("ga_app_vendor", out, on_conflict="track_id")

    log.info("enrich done: %s", stats.as_dict())
    return stats


def refresh_known_apps() -> dict:
    """Monthly step 1: batch-lookup every known track_id.

    Catches delisting, version bumps and metadata edits in ~70 calls at 200 ids each
    (the plan budgeted ~350 at 100). The batch size is asserted inside the client
    because Apple truncates silently above 200.

    An id absent on the first pass gets ONE retry, config.DELIST_RETRY_DELAY_S
    later, before being finalized as delisted (Derek, 2026-09-11: on a monthly
    cadence, one bad Apple API response shouldn't be enough to mark a real, live
    app dead for a whole month). Every id that DOES come back -- first pass or
    retry -- has delisted_at explicitly cleared in the same upsert, so an app that
    reappears after a prior month's delisting gets un-delisted instead of staying
    stuck forever (app_row() itself never touches delisted_at, so without this the
    column would just keep whatever a prior run last set).
    """
    from . import itunes

    ids = sorted(db.known_track_ids())
    if not ids:
        return {"checked": 0}
    client = itunes.ITunesClient()

    def _lookup_pass(batch_ids: list[int], check_truncation: bool = True) -> set[int]:
        seen: set[int] = set()
        for batch in client.lookup_many(batch_ids, check_truncation=check_truncation):
            rows = [itunes.app_row(r) | {"delisted_at": None} for r in batch]
            if rows:
                db.upsert("ga_app", rows, on_conflict="track_id")
                seen |= {r["track_id"] for r in rows}
        return seen

    seen = _lookup_pass(ids)
    gone = [i for i in ids if i not in seen]

    confirmed_on_retry = 0
    if gone:
        log.info("refresh: %d absent on first pass, retrying in %ds",
                 len(gone), config.DELIST_RETRY_DELAY_S)
        time.sleep(config.DELIST_RETRY_DELAY_S)
        # check_truncation=False: this batch is ALREADY pre-filtered to ids
        # missing from pass 1, so a high (even 100%) still-missing fraction here
        # is the expected outcome, not evidence of truncation (see
        # ITunesClient.lookup_batch's docstring).
        retry_seen = _lookup_pass(gone, check_truncation=False)
        seen |= retry_seen
        confirmed_on_retry = len(retry_seen)
        gone = [i for i in gone if i not in retry_seen]

    if gone:
        db.upsert("ga_app",
                  [{"track_id": i, "delisted_at": appstore._utcnow()} for i in gone],
                  on_conflict="track_id")
    return {"checked": len(ids), "present": len(seen), "delisted": len(gone),
            "confirmed_on_retry": confirmed_on_retry, "calls": client.calls}
