"""Reporting: app penetration, vendor market share, blind-spot metrics, monthly diff."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone

from . import config, db, vendors


def penetration() -> dict:
    total = db.facility_count()
    with_app = db.query("""
        SELECT count(DISTINCT facility_id) n FROM ga_facility_app
    """)[0]["n"]
    return {
        "facilities_total": total,
        "facilities_with_app": with_app,
        "facilities_without_app": total - with_app,
        "penetration_pct": round(with_app / total * 100, 2) if total else 0.0,
    }


def market_share() -> dict:
    reg = vendors.load()
    rows = db.query("""
        SELECT v.vendor, v.confidence, count(DISTINCT fa.facility_id) facilities,
               count(DISTINCT v.track_id) apps
        FROM ga_app_vendor v
        LEFT JOIN ga_facility_app fa ON fa.track_id = v.track_id
        WHERE v.vendor IS NOT NULL
        GROUP BY 1, 2
        ORDER BY facilities DESC
    """)
    by_vendor: dict[str, dict] = {}
    for r in rows:
        e = by_vendor.setdefault(r["vendor"], {
            "display_name": reg.vendors[r["vendor"]].display_name
            if r["vendor"] in reg.vendors else r["vendor"],
            "category": reg.vendors[r["vendor"]].category
            if r["vendor"] in reg.vendors else "unknown",
            "facilities": 0, "apps": 0, "by_confidence": {},
        })
        e["facilities"] += r["facilities"] or 0
        e["apps"] += r["apps"] or 0
        e["by_confidence"][r["confidence"]] = r["apps"]

    booking = {k: v for k, v in by_vendor.items() if v["category"] == "booking"}
    portal = {k: v for k, v in by_vendor.items() if v["category"] == "club_portal"}
    return {
        "by_vendor": dict(sorted(by_vendor.items(),
                                 key=lambda kv: -kv[1]["facilities"])),
        "booking_total_facilities": sum(v["facilities"] for v in booking.values()),
        "club_portal_total_facilities": sum(v["facilities"] for v in portal.values()),
    }


def blind_spots() -> dict:
    """The honest measure of what this pipeline cannot see.

    Two distinct kinds, and conflating them would be misleading:
      * `unknown_vendor_apps` -- golf booking apps we found but could not attribute.
        A detection gap; more fingerprints would close it.
      * `structurally_undetectable_vendors` -- vendors that ship ONE multi-tenant app,
        so their client courses have no listing to find. No amount of crawling helps.
        These MUST NOT be reported as ~0% market share.
    """
    reg = vendors.load()
    unknown = db.query("""
        SELECT count(*) n FROM ga_app_vendor WHERE vendor IS NULL
    """)[0]["n"]
    low = db.query("""
        SELECT count(*) n FROM ga_app_vendor WHERE confidence = 'low'
    """)[0]["n"]
    conflicts = db.query("""
        SELECT count(*) n FROM ga_app_vendor WHERE conflict
    """)[0]["n"]

    undetectable = [
        {"vendor": k, "display_name": reg.vendors[k].display_name,
         "why": reg.vendors[k].evidence.strip()}
        for k in vendors.undetectable_vendors(reg)
    ]
    return {
        "unknown_vendor_apps": unknown,
        "low_confidence_apps": low,
        "listings_naming_two_vendors": conflicts,
        "structurally_undetectable_vendors": undetectable,
        "caveat": (
            "Vendors listed as structurally undetectable ship a single multi-tenant "
            "app; their client courses have no per-facility App Store listing. Their "
            "market share here is a floor, not an estimate."
        ),
    }


def vendor_switches() -> list[dict]:
    """Listings whose fields name different vendors -- migrations caught mid-flight."""
    return db.query("""
        SELECT v.track_id, a.track_name, v.vendor, v.confidence, v.conflict_detail
        FROM ga_app_vendor v JOIN ga_app a ON a.track_id = v.track_id
        WHERE v.conflict
        ORDER BY a.track_name
        LIMIT 200
    """)


def full_report() -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "penetration": penetration(),
        "market_share": market_share(),
        "blind_spots": blind_spots(),
    }


# ---------------------------------------------------------------- snapshots
def write_snapshot(run_id: str | None = None) -> dict:
    """Freeze the CURRENT ga_facility_app state into this run's row set --
    only apps confirmed alive. Filtered on ga_app.delisted_at IS NULL (Derek,
    2026-09-12): the snapshot is "who we think currently has an ACTIVE app",
    not just "who has a link on file" -- a dead app shouldn't sit in this
    census just because its crosswalk link is still on the books.
    ga_facility_app itself is deliberately left untouched by this filter --
    it's the durable facility<->app pairing, not a liveness census; the
    filter belongs here, at the point the census gets taken.

    Also deletes any row ALREADY in this run_id for an app now confirmed
    delisted (found immediately after adding the filter above: a prior
    write_snapshot() call earlier the same month had recorded 23 apps as
    alive before they were known dead, and the filtered INSERT alone never
    retroactively removes a stale row -- it only stops adding new ones).
    Scoped strictly to this run_id; past months' rows are historical record
    and are never touched, even for an app that's since died.
    """
    run_id = run_id or f"run_{date.today():%Y%m}"
    db.exec_sql(f"""
        DELETE FROM ga_snapshot s
        USING ga_app a
        WHERE s.track_id = a.track_id AND a.delisted_at IS NOT NULL
          AND s.run_id = {db.lit(run_id)}
    """)
    db.exec_sql(f"""
        INSERT INTO ga_snapshot (run_id, run_date, facility_id, track_id, vendor,
                                  confidence, match_method)
        SELECT {db.lit(run_id)}, current_date, fa.facility_id, fa.track_id,
               v.vendor, v.confidence, fa.match_method
        FROM ga_facility_app fa
        JOIN ga_app a ON a.track_id = fa.track_id AND a.delisted_at IS NULL
        LEFT JOIN ga_app_vendor v ON v.track_id = fa.track_id
        ON CONFLICT (run_id, facility_id, track_id) DO UPDATE
          SET vendor = excluded.vendor, confidence = excluded.confidence,
              match_method = excluded.match_method
    """)
    n = db.query(f"SELECT count(*) n FROM ga_snapshot WHERE run_id = {db.lit(run_id)}")
    return {"run_id": run_id, "rows": n[0]["n"] if n else 0}


def new_review_items(run_id: str) -> list[dict]:
    """Outcome #5 of the monthly report -- vendor-confirmed apps flagged for
    human review for the FIRST time this run. These never appear in
    ga_snapshot at all (no facility link yet), so diff() alone can't see
    them -- this is the only place they show up."""
    return db.query(f"""
        SELECT track_id, app_name, vendor, app_store_url
        FROM ga_review_queue
        WHERE first_flagged_run_id = {db.lit(run_id)} AND status = 'pending'
        ORDER BY app_name
    """)


def diff(since_run: str, until_run: str | None = None) -> dict:
    """Month-over-month change -- the five outcomes Derek tracks:
      1. unchanged        -- same facility_id + vendor in both runs (count only,
                              nothing actionable to list).
      2. vendor_lost       -- still linked to the same facility, vendor no
                              longer detected (vendor -> NULL).
      3. delisted_apps     -- facility_id present in `since_run`, gone from
                              `until_run`. confirmed_delisted is only true when
                              ga_app.delisted_at backs it up (the retried,
                              confirmed case) -- otherwise the link changed for
                              some other reason (a crosswalk edit, the facility
                              fell out of scope), not an actual dead app.
      4. new_apps          -- facility_id newly present; match_method tells an
                              auto-matched link (anything but manual_crosswalk)
                              apart from a Retool-resolved review item
                              (manual_crosswalk).
      5. new_review_items()-- apps flagged for human review for the first time
                              this run (see that function).
    vendor_switched (a real vendor migration, not a loss) is reported too, since
    it's a natural byproduct of the same query, even though it isn't one of the
    five named outcomes.
    """
    runs = db.query("SELECT DISTINCT run_id FROM ga_snapshot ORDER BY run_id DESC")
    ids = [r["run_id"] for r in runs]
    if not until_run:
        until_run = ids[0] if ids else since_run

    new_apps = db.query(f"""
        SELECT b.facility_id, b.track_id, b.vendor, b.match_method
        FROM ga_snapshot b
        WHERE b.run_id = {db.lit(until_run)}
          AND NOT EXISTS (SELECT 1 FROM ga_snapshot a
                          WHERE a.run_id = {db.lit(since_run)}
                            AND a.facility_id = b.facility_id)
    """)
    lost_apps = db.query(f"""
        SELECT a.facility_id, a.track_id, a.vendor
        FROM ga_snapshot a
        WHERE a.run_id = {db.lit(since_run)}
          AND NOT EXISTS (SELECT 1 FROM ga_snapshot b
                          WHERE b.run_id = {db.lit(until_run)}
                            AND b.facility_id = a.facility_id)
    """)
    if lost_apps:
        delisted_ids = {r["track_id"] for r in db.query(
            "SELECT track_id FROM ga_app WHERE delisted_at IS NOT NULL AND track_id IN ("
            + ",".join(str(r["track_id"]) for r in lost_apps) + ")")}
        for r in lost_apps:
            r["confirmed_delisted"] = r["track_id"] in delisted_ids

    # Joined on (facility_id, track_id), NOT facility_id alone: a facility with
    # more than one linked app (real -- multi-course apps, crosswalk arrays) would
    # otherwise cross-join every one of its apps against every other, and flag
    # untouched pairs as false "switches". Found 2026-09-11 diffing a run against
    # itself: facility-only join produced 210 phantom switches with nothing
    # actually different between the two sides.
    vendor_lost = db.query(f"""
        SELECT a.facility_id, a.track_id, a.vendor AS from_vendor
        FROM ga_snapshot a
        JOIN ga_snapshot b ON b.facility_id = a.facility_id AND b.track_id = a.track_id
        WHERE a.run_id = {db.lit(since_run)} AND b.run_id = {db.lit(until_run)}
          AND a.vendor IS NOT NULL AND b.vendor IS NULL
    """)
    vendor_switched = db.query(f"""
        SELECT a.facility_id, a.track_id, a.vendor AS from_vendor, b.vendor AS to_vendor
        FROM ga_snapshot a
        JOIN ga_snapshot b ON b.facility_id = a.facility_id AND b.track_id = a.track_id
        WHERE a.run_id = {db.lit(since_run)} AND b.run_id = {db.lit(until_run)}
          AND a.vendor IS NOT NULL AND b.vendor IS NOT NULL AND a.vendor != b.vendor
    """)
    unchanged_n = db.query(f"""
        SELECT count(*) n
        FROM ga_snapshot a
        JOIN ga_snapshot b ON b.facility_id = a.facility_id AND b.track_id = a.track_id
        WHERE a.run_id = {db.lit(since_run)} AND b.run_id = {db.lit(until_run)}
          AND a.vendor IS NOT DISTINCT FROM b.vendor
    """)[0]["n"]
    review_items = new_review_items(until_run)

    return {
        "since": since_run, "until": until_run,
        "new_apps": new_apps, "delisted_apps": lost_apps,
        "vendor_lost": vendor_lost, "vendor_switched": vendor_switched,
        "new_review_items": review_items,
        "counts": {
            "unchanged": unchanged_n,
            "new": len(new_apps), "delisted": len(lost_apps),
            "vendor_lost": len(vendor_lost), "vendor_switched": len(vendor_switched),
            "new_needs_review": len(review_items),
        },
    }


def render_text(rep: dict) -> str:
    p, m, b = rep["penetration"], rep["market_share"], rep["blind_spots"]
    lines = [
        "=" * 72,
        "GOLF FACILITY APP / VENDOR REPORT",
        f"generated {rep['generated_at']}",
        "=" * 72,
        "",
        "PENETRATION",
        f"  facilities in scope : {p['facilities_total']:,}",
        f"  with a mobile app   : {p['facilities_with_app']:,} "
        f"({p['penetration_pct']}%)",
        f"  without             : {p['facilities_without_app']:,}",
        "",
        "VENDOR MARKET SHARE",
        f"  {'vendor':26s} {'category':13s} {'facilities':>10s} {'apps':>7s}",
    ]
    for key, v in m["by_vendor"].items():
        lines.append(f"  {v['display_name'][:25]:26s} {v['category']:13s} "
                     f"{v['facilities']:>10,} {v['apps']:>7,}")
    lines += [
        "",
        f"  booking vendors total     : {m['booking_total_facilities']:,} facilities",
        f"  club portal vendors total : {m['club_portal_total_facilities']:,} facilities",
        "",
        "BLIND SPOTS",
        f"  golf apps with no vendor signal : {b['unknown_vendor_apps']:,}",
        f"  low-confidence labels           : {b['low_confidence_apps']:,}",
        f"  listings naming two vendors     : {b['listings_naming_two_vendors']:,}",
        "",
        "  Structurally undetectable vendors (single multi-tenant app -- their",
        "  client courses have NO per-facility listing; share below is a floor):",
    ]
    for u in b["structurally_undetectable_vendors"]:
        lines.append(f"    - {u['display_name']}")
    return "\n".join(lines)
