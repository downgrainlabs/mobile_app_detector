"""The Retool review queue -- what rebuild.run() could not resolve on its own.

Populated at the end of every rebuild.run() from its `unresolved` pool (vendor-
confirmed, live, not in app_crosswalk, no domain/OCR/subtitle/name match found).
A human resolves each row in Retool; resolve_review_item() (schema.sql) is the
one atomic write-back that both writes app_crosswalk and flips this table's
status, so the two can never disagree about whether an app's been reviewed.

Design constraints carried over from worksheet.py (the CSV-based precursor of
this same review-queue idea):
  * Keyed on track_id, never name.
  * Carry enough context (candidates, owner suggestion) that a reviewer never
    has to open the App Store just to make a call.

A track_id already flagged as `pending` and still unresolved keeps its original
first_flagged_run_id forever -- that's the only way "new this run" (outcome #5
of the monthly report) can be told apart from a growing backlog.
"""
from __future__ import annotations

import logging

from . import db, worksheet

log = logging.getLogger(__name__)


def _candidates_for(track_ids: list[int]) -> dict[int, list[dict]]:
    """Up to 3 facility candidates per app, from ga_match_ambiguous -- same
    source worksheet.py's CSV export uses, just scoped to the given ids."""
    if not track_ids:
        return {}
    ids = ",".join(str(t) for t in track_ids)
    amb = {r["track_id"]: r["candidates"] or [] for r in db.query(
        f"SELECT track_id, candidates FROM ga_match_ambiguous WHERE track_id IN ({ids})")}

    fac_ids = sorted({c["facility_id"] for cs in amb.values()
                      for c in cs if c.get("facility_id")})
    fac: dict[int, dict] = {}
    for i in range(0, len(fac_ids), 400):
        chunk = ",".join(str(x) for x in fac_ids[i:i + 400])
        for f in db.query(
            "SELECT facility_id, facility_name, city, state_code FROM facility "
            f"WHERE facility_id IN ({chunk})"
        ):
            fac[f["facility_id"]] = f

    out: dict[int, list[dict]] = {}
    for tid, cs in amb.items():
        top = [fac[c["facility_id"]] for c in cs if c.get("facility_id") in fac][:3]
        out[tid] = [{"facility_id": f["facility_id"], "facility_name": f["facility_name"],
                     "city": f.get("city"), "state_code": f.get("state_code")} for f in top]
    return out


def sync(unresolved: dict[int, dict], run_id: str) -> dict:
    """unresolved: {track_id: app_dict} left over after rebuild.run()'s full
    waterfall -- the exact dict rebuild.py already builds, keyed the same way.
    Call this once per rebuild.run(), after the waterfall settles."""
    pending = {r["track_id"] for r in db.query(
        "SELECT track_id FROM ga_review_queue WHERE status = 'pending'")}
    now_crosswalked = {r["track_id"] for r in db.query("SELECT track_id FROM app_crosswalk")}
    now_delisted = {r["track_id"] for r in db.query(
        "SELECT track_id FROM ga_app WHERE delisted_at IS NOT NULL")}

    # A pending row leaves the queue for one of two reasons: someone (Retool or
    # the pipeline itself) resolved it into app_crosswalk, or the app died before
    # anyone got to it. Either way, if it's still in `unresolved` it isn't
    # actually settled -- that can't happen today (apply_crosswalk() excludes
    # crosswalked ids from the pool before `unresolved` is even built) but the
    # ordering guards against it anyway.
    resolved_ids = (pending & now_crosswalked) - set(unresolved)
    delisted_ids = (pending & now_delisted) - resolved_ids - set(unresolved)
    if resolved_ids:
        db.exec_sql(
            "UPDATE ga_review_queue SET status = 'resolved', resolved_at = now(), "
            "resolved_by = COALESCE(resolved_by, 'pipeline') "
            f"WHERE track_id IN ({','.join(str(t) for t in resolved_ids)}) "
            "AND status = 'pending'")
    if delisted_ids:
        db.exec_sql(
            "UPDATE ga_review_queue SET status = 'delisted' "
            f"WHERE track_id IN ({','.join(str(t) for t in delisted_ids)}) "
            "AND status = 'pending'")

    if not unresolved:
        return {"resolved": len(resolved_ids), "delisted": len(delisted_ids),
                "pending": 0, "new": 0}

    owners = worksheet._owner_index()
    cands = _candidates_for(list(unresolved))
    already_flagged = {r["track_id"]: r["first_flagged_run_id"] for r in db.query(
        "SELECT track_id, first_flagged_run_id FROM ga_review_queue "
        f"WHERE track_id IN ({','.join(str(t) for t in unresolved)})")}

    rows = []
    for tid, a in unresolved.items():
        rows.append({
            "track_id": tid,
            "app_name": a.get("track_name"),
            "vendor": a.get("vendor"),
            "confidence": a.get("confidence"),
            "bundle_id": a.get("bundle_id"),
            "seller_name": a.get("seller_name"),
            "subtitle": a.get("subtitle"),
            "app_store_url": f"https://apps.apple.com/us/app/id{tid}",
            "description": (a.get("description") or "")[:1000],
            "candidates": cands.get(tid, []),
            "owner_suggestion": worksheet._suggest_owner(a.get("seller_name"), owners),
            "status": "pending",
            "first_flagged_run_id": already_flagged.get(tid, run_id),
            "last_seen_run_id": run_id,
        })
    db.upsert("ga_review_queue", rows, on_conflict="track_id")

    new_n = sum(1 for tid in unresolved if tid not in already_flagged)
    log.info("review_queue: %d resolved, %d delisted, %d pending (%d new this run)",
             len(resolved_ids), len(delisted_ids), len(rows), new_n)
    return {"resolved": len(resolved_ids), "delisted": len(delisted_ids),
            "pending": len(rows), "new": new_n}
