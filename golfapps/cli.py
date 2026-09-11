"""golfapps CLI."""
from __future__ import annotations

import argparse
import json
import logging
import sys


def _log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="golfapps",
                                description="Golf facility -> app -> vendor pipeline")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("initdb", help="create the ga_* tables (idempotent)")

    sp = sub.add_parser("sweep", help="Phase 1: build the app corpus")
    sp.add_argument("--limit", type=int, help="cap queries (smoke test)")
    sp.add_argument("--no-geo", action="store_true", help="skip the geographic sweep")

    se = sub.add_parser("enrich", help="Phase 3: label vendors, escalate to HTML")
    se.add_argument("--no-escalate", action="store_true",
                    help="label from free JSON only, never fetch HTML")
    se.add_argument("--max-html", type=int, help="cap HTML fetches")
    se.add_argument("--html-for-matching", action="store_true",
                    help="fetch HTML for every course app, not just unlabelled ones "
                         "(populates support_url/privacy_policy_url for domainmatch)")

    sj = sub.add_parser("join", help="Phase 2: match apps to facilities")
    sj.add_argument("--limit", type=int)
    sj.add_argument("--threshold", type=float, default=0.85)

    sub.add_parser("calibrate", help="measure recall against known pairs")
    sd2 = sub.add_parser("domainmatch",
                         help="Phase 2b: link apps to facilities by website domain")
    sd2.add_argument("--dry-run", action="store_true")
    sub.add_parser("triage", help="bucket the residue and size the manual queue")
    sst = sub.add_parser("subtitle", help="Phase 2d: resolve via App Store subtitle "
                                          "(any vendor -- location text, not vendor-specific)")
    ssto = sub.add_parser("ocr", help="Phase 2e: resolve via screenshot OCR "
                                      "(vetted-safe vendors only -- screenshot_ocr.SAFE_VENDORS)")
    sn = sub.add_parser("needs-lens", help="export the standing pre-Lens needs-lens file "
                                           "(the real one -- filtered through classify())")
    spl = sub.add_parser("pipeline", help="THE canonical pipeline (rebuild.run()): "
                                          "crosswalk -> non-US filter -> domain -> OCR -> "
                                          "crawl -> domain -> subtitle -> name+state match. "
                                          "Replaces ga_facility_app fresh every run. No Lens step "
                                          "-- Lens output only becomes real via app_crosswalk.")
    spl.add_argument("--dry-run", action="store_true",
                     help="compute but do not write links")
    spl.add_argument("--discover", action="store_true",
                     help="also run the facility-name discovery sweep first "
                          "(core name per unlinked facility -- real query volume, "
                          "needs --proxy at any real scale)")
    spl.add_argument("--proxy", action="store_true",
                     help="route discovery + HTML crawl through Evomi "
                          "(config.proxy_config(), needs EVOMI_* in .env)")
    sx = sub.add_parser("worksheet", help="export the manual review CSV")
    sx.add_argument("--out", default="docs/review_worksheet.csv")
    sx.add_argument("--slim", action="store_true",
                    help="just app name, URL, FACILITY_ID, OWNER_ID")
    sv = sub.add_parser("verify", help="export the VERIFY queue (is this link right?)")
    sv.add_argument("--out", default="docs/verify_queue.csv")
    svi = sub.add_parser("verify-import", help="apply confirmations/corrections")
    svi.add_argument("path")
    svi.add_argument("--dry-run", action="store_true")
    si = sub.add_parser("worksheet-import", help="read a filled worksheet back in")
    si.add_argument("path")
    si.add_argument("--dry-run", action="store_true")
    sq = sub.add_parser("whois", help="who makes the app for a given course?")
    sq.add_argument("name")
    sq.add_argument("--city")
    sq.add_argument("--state")
    sw = sub.add_parser("webdetect",
                        help="Phase 2c: identify clubs from their app logo via Cloud "
                             "Vision WEB_DETECTION, resolve to a domain, then to a "
                             "facility")
    sw.add_argument("--limit", type=int, help="cap API calls (start small)")
    sw.add_argument("--dry-run", action="store_true", help="do not write links")
    sub.add_parser("refresh", help="batch re-lookup every known app (delisting check)")

    sr = sub.add_parser("report", help="penetration + market share + blind spots")
    sr.add_argument("--json", action="store_true")

    ss = sub.add_parser("snapshot", help="freeze current state for diffing")
    ss.add_argument("--run-id")

    sd = sub.add_parser("diff", help="month-over-month change")
    sd.add_argument("--since", required=True, help="run_id, e.g. run_202608")
    sd.add_argument("--until")

    sm = sub.add_parser("monthly", help="orchestrate the recurring run: both "
                                        "sweeps, the canonical waterfall "
                                        "(rebuild.run()), snapshot, ga_run "
                                        "bookkeeping -- the Render entrypoint")
    sm.add_argument("--run-id")
    sm.add_argument("--skip-sweep", action="store_true",
                    help="skip the generic/geo/vendor-term sweep -- "
                         "rebuild.run(run_discovery=True) still runs the "
                         "facility-name sweep regardless")
    sm.add_argument("--no-proxy", dest="proxy", action="store_false", default=True,
                    help="do not route discovery/crawl through Evomi -- proxy "
                         "is on by default, real query volume needs it")

    sub.add_parser("vendors", help="show loaded vendor fingerprints")

    a = p.parse_args(argv)
    _log(a.verbose)

    # Imported lazily so `golfapps vendors` works without database credentials.
    from . import (calibrate, db, domainmatch, enrich, join, lens, rebuild, report,
                   screenshot_ocr, subtitle_resolve, sweep, triage, vendors, webdetect)
    from . import lookup, verify, worksheet

    def _unlinked_candidate_ids() -> list[int]:
        """The base pool every pre-Lens layer draws from: vendor-confirmed,
        no facility link, not already excluded/crosswalked. Re-queried fresh
        each call so each pipeline stage sees the PREVIOUS stage's results."""
        rows = db.query("""
            SELECT a.track_id FROM ga_app a
            JOIN ga_app_vendor v ON v.track_id = a.track_id AND v.vendor IS NOT NULL
            LEFT JOIN ga_facility_app fa ON fa.track_id = a.track_id
            LEFT JOIN app_crosswalk cw ON cw.track_id = a.track_id
            WHERE a.delisted_at IS NULL AND a.not_a_golf_course_at IS NULL
              AND a.canadian_course_at IS NULL AND fa.track_id IS NULL
              AND cw.track_id IS NULL
        """)
        return [r["track_id"] for r in rows]

    if a.cmd == "initdb":
        db.init_schema()
        print("schema applied")
        print(f"facilities in scope: {db.facility_count():,}")

    elif a.cmd == "sweep":
        s = sweep.run(limit=a.limit, include_geo=not a.no_geo)
        print(json.dumps(s.as_dict(), indent=2))

    elif a.cmd == "enrich":
        s = enrich.run(escalate=not a.no_escalate, max_html=a.max_html,
                       html_for_matching=a.html_for_matching)
        print(json.dumps(s.as_dict(), indent=2))

    elif a.cmd == "join":
        s = join.run(threshold=a.threshold, limit=a.limit)
        print(json.dumps(s.as_dict(), indent=2))

    elif a.cmd == "domainmatch":
        print(json.dumps(domainmatch.run(apply_links=not a.dry_run).as_dict(),
                         indent=2, default=str))

    elif a.cmd == "triage":
        print(triage.render(triage.run()))

    elif a.cmd == "subtitle":
        pool = _unlinked_candidate_ids()
        result = subtitle_resolve.run(pool)
        if result["resolved"]:
            db.upsert("ga_facility_app", result["resolved"], on_conflict="facility_id,track_id")
        print(json.dumps({"considered": result["considered"],
                          "resolved": len(result["resolved"])}, indent=2))

    elif a.cmd == "ocr":
        pool = _unlinked_candidate_ids()
        result = screenshot_ocr.run(pool)
        if result["resolved"]:
            db.upsert("ga_facility_app", result["resolved"], on_conflict="facility_id,track_id")
        print(json.dumps({"considered": result["considered"],
                          "resolved": len(result["resolved"])}, indent=2))

    elif a.cmd == "needs-lens":
        print(json.dumps(lens.export_needs_lens(), indent=2))

    elif a.cmd == "pipeline":
        stats = rebuild.run(apply_links=not a.dry_run, run_discovery=a.discover,
                            use_proxy=a.proxy)
        print(json.dumps(stats.as_dict(), indent=2, default=str))

    elif a.cmd == "webdetect":
        print(json.dumps(webdetect.run(limit=a.limit,
                                       apply_links=not a.dry_run).as_dict(),
                         indent=2, default=str))

    elif a.cmd == "worksheet":
        print(json.dumps(worksheet.export(a.out, slim=a.slim), indent=2))

    elif a.cmd == "verify":
        print(json.dumps(verify.export(a.out), indent=2))

    elif a.cmd == "verify-import":
        print(json.dumps(verify.import_decisions(a.path, a.dry_run), indent=2))

    elif a.cmd == "worksheet-import":
        print(json.dumps(worksheet.import_decisions(a.path, a.dry_run), indent=2))

    elif a.cmd == "whois":
        print(lookup.render(lookup.ask(a.name, a.city, a.state)))

    elif a.cmd == "calibrate":
        out = calibrate.run()
        return 0 if out.get("totals", {}).get("recall_pct", 0) >= 70 else 1

    elif a.cmd == "refresh":
        print(json.dumps(enrich.refresh_known_apps(), indent=2))

    elif a.cmd == "report":
        rep = report.full_report()
        print(json.dumps(rep, indent=2, default=str) if a.json
              else report.render_text(rep))

    elif a.cmd == "snapshot":
        print(json.dumps(report.write_snapshot(a.run_id), indent=2))

    elif a.cmd == "diff":
        print(json.dumps(report.diff(a.since, a.until), indent=2, default=str))

    elif a.cmd == "monthly":
        from datetime import date, datetime, timezone
        run_id = a.run_id or f"run_{date.today():%Y%m}"
        db.upsert("ga_run", [{"run_id": run_id, "phase": "start"}], on_conflict="run_id")
        try:
            if not a.skip_sweep:
                print(f"1/3 sweep (generic/geo/vendor terms) -- run {run_id}")
                sweep_stats = sweep.run(include_geo=True).as_dict()
            else:
                print("1/3 sweep skipped (--skip-sweep)")
                sweep_stats = None
            print(json.dumps(sweep_stats))

            print("2/3 rebuild.run() -- facility-name discovery + full waterfall "
                  "+ review queue sync")
            rebuild_stats = rebuild.run(apply_links=True, run_discovery=True,
                                        use_proxy=a.proxy, run_id=run_id)
            print(json.dumps(rebuild_stats.as_dict(), default=str))

            print("3/3 snapshot")
            snap = report.write_snapshot(run_id)
            print(json.dumps(snap))

            db.upsert("ga_run", [{
                "run_id": run_id, "phase": "done",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "stats": {"sweep": sweep_stats, "rebuild": rebuild_stats.as_dict(),
                          "snapshot": snap},
            }], on_conflict="run_id")
        except Exception as e:
            db.upsert("ga_run", [{
                "run_id": run_id, "phase": "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "stats": {"error": str(e)},
            }], on_conflict="run_id")
            raise

    elif a.cmd == "vendors":
        reg = vendors.load()
        print(f"{len(reg.vendors)} vendors loaded from {__import__('golfapps.config',
              fromlist=['x']).VENDORS_YAML}")
        print(f"\n{'key':17s} {'category':12s} {'model':16s} {'detectable':11s} bundles")
        for k, v in reg.vendors.items():
            print(f"{k:17s} {v.category:12s} {v.publishing_model:16s} "
                  f"{str(v.detectable_per_facility):11s} "
                  f"{','.join(v.bundle_prefixes) or '-'}")
        und = vendors.undetectable_vendors(reg)
        if und:
            print(f"\nNo per-facility listing exists for: {', '.join(und)}")
            print("Their market share is a floor, not an estimate.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
