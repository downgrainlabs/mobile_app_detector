"""The VERIFY queue -- "here is a proposed facility, is it right?"

Distinct from the RESOLVE queue (worksheet.py), which asks "which facility is this?"
Verify is smaller but higher value per row: a wrong link actively corrupts market-share
numbers, while a missing link is merely absent. Rancho Bernardo sat linked to an indoor
golf bar at confidence 1.00 and looked perfectly healthy.

Three sources, all reduced to the same question so the reviewer holds one mental model:

  * strong_suggestion -- the match service said "No Match" but returned a single
    candidate at >=0.90. 151 of these are vendor-labelled real course apps. NOT
    auto-accepted, because the same bucket contains "New South Wales Golf Club" ->
    "South Wales Golf Club". One glance each.
  * weak_link         -- a link exists but app and facility names barely overlap.
    Genuinely mixed: "My HSCC" -> Highland Springs Country Club is correct (initials),
    "Golf King - World Tour" -> Kings Country Club is a video game.
  * competing         -- a link exists but other candidates were also plausible.

Links confirmed by BOTH the name and domain channels are deliberately EXCLUDED -- two
pieces of independent evidence is the strongest signal available here, and re-reviewing
692 of those would waste the reviewer's attention.
"""
from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

from . import config, db

log = logging.getLogger(__name__)

HEADER = ["track_id", "source", "flag_reason", "app_name", "app_store_url",
          "vendor", "proposed_facility", "name_agreement",
          "CONFIRM [y/n]", "CORRECTED_FACILITY_ID", "NOTES"]

_STOP = {"golf", "club", "course", "resort", "country", "links", "the", "at", "and",
         "gc", "cc", "app", "tee", "times", "inc", "llc", "of"}
LOW_AGREEMENT = 0.34

# The mere existence of other candidates is NOT a reason to review. 320 flagged rows
# had perfect name agreement -- "Somerby Golf Club" -> "Somerby Golf Club" with
# alternatives present is not in doubt, and asking someone to confirm 320 of those
# burns the attention this queue exists to spend well. Competing candidates only
# matter when the chosen name is not itself a clear match.
COMPETING_MAX_AGREEMENT = 0.70


def _toks(s: str | None) -> set[str]:
    return {t for t in re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split()
            if t and t not in _STOP}


def agreement(a: str | None, b: str | None) -> float:
    ta, tb = _toks(a), _toks(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def _fac_label(f: dict) -> str:
    return (f"{f['facility_id']} | {f['facility_name']} | "
            f"{f.get('city') or '?'}, {f.get('state_code') or '?'}")


def build_rows() -> list[dict]:
    rows: list[dict] = []

    # ---- source 1: strong single suggestion on a "No Match" -----------------
    amb = db.query("""
        SELECT m.track_id, m.submitted_name, m.candidates, a.track_name, v.vendor
        FROM ga_match_ambiguous m
        JOIN ga_app a ON a.track_id = m.track_id
        LEFT JOIN ga_app_vendor v ON v.track_id = m.track_id
        WHERE m.match_status = 'No Match'
          AND NOT EXISTS (SELECT 1 FROM ga_facility_app f WHERE f.track_id = m.track_id)
          AND NOT EXISTS (SELECT 1 FROM ga_app_disposition d WHERE d.track_id = m.track_id)
    """)
    want_ids = set()
    strong: list[tuple[dict, dict]] = []
    for r in amb:
        cands = r["candidates"] or []
        if not cands:
            continue
        top = max(cands, key=lambda c: c.get("confidence") or 0)
        if (top.get("confidence") or 0) < 0.90:
            continue
        if not r.get("vendor"):
            continue          # unlabelled: mostly consumer apps, belongs in RESOLVE
        strong.append((r, top))
        want_ids.add(top["facility_id"])

    # ---- source 2 & 3: existing links with a warning sign -------------------
    links = db.query("""
        SELECT fa.track_id, fa.facility_id, fa.match_method, fa.match_status,
               a.track_name, v.vendor,
               f.facility_name, f.city, f.state_code,
               (SELECT count(*) FROM ga_facility_app x WHERE x.track_id = fa.track_id) n_fac,
               (SELECT count(*) FROM ga_facility_app y WHERE y.facility_id = fa.facility_id) n_app,
               (SELECT count(*) FROM ga_match_ambiguous m
                  WHERE m.track_id = fa.track_id AND m.candidate_count > 1) had_alts,
               (SELECT count(*) FROM ga_facility_app z
                  WHERE z.track_id = fa.track_id AND z.match_method = 'domain') has_domain
        FROM ga_facility_app fa
        JOIN ga_app a ON a.track_id = fa.track_id
        JOIN facility f ON f.facility_id = fa.facility_id
        LEFT JOIN ga_app_vendor v ON v.track_id = fa.track_id
    """)

    # Corroborated by two independent channels -> trust, do not review.
    corroborated = {r["track_id"] for r in links
                    if r["has_domain"] and r["match_method"] != "domain"}

    facs: dict[int, dict] = {}
    if want_ids:
        ids = sorted(want_ids)
        for i in range(0, len(ids), 400):
            chunk = ",".join(str(x) for x in ids[i:i + 400])
            for f in db.query(
                "SELECT facility_id, facility_name, city, state_code, active_status, "
                f"facility_type FROM facility WHERE facility_id IN ({chunk})"
            ):
                facs[f["facility_id"]] = f

    for r, top in strong:
        f = facs.get(top["facility_id"])
        if not f:
            continue
        rows.append({
            "track_id": r["track_id"], "source": "strong_suggestion",
            "flag_reason": f"service said No Match but scored this {top.get('confidence')}",
            "app_name": r["track_name"],
            "app_store_url": f"https://apps.apple.com/us/app/id{r['track_id']}",
            "vendor": r.get("vendor") or "",
            "proposed_facility": _fac_label(f),
            "name_agreement": f"{agreement(r['track_name'], f['facility_name']):.2f}",
            "CONFIRM [y/n]": "", "CORRECTED_FACILITY_ID": "", "NOTES": "",
            "_sort": (0, -(top.get("confidence") or 0)),
        })

    for r in links:
        if r["track_id"] in corroborated:
            continue
        ag = agreement(r["track_name"], r["facility_name"])
        reasons = []
        if ag < LOW_AGREEMENT:
            reasons.append(f"app/facility names barely overlap ({ag:.2f})")
        if r["had_alts"] and ag < COMPETING_MAX_AGREEMENT:
            reasons.append(f"other candidates were plausible and the name is only a "
                           f"partial match ({ag:.2f})")
        if r["n_fac"] > 1:
            reasons.append(f"app links to {r['n_fac']} facilities")
        if r["n_app"] > 1:
            reasons.append(f"facility has {r['n_app']} apps")
        if not reasons:
            continue
        rows.append({
            "track_id": r["track_id"],
            "source": "weak_link" if ag < LOW_AGREEMENT else "competing",
            "flag_reason": "; ".join(reasons),
            "app_name": r["track_name"],
            "app_store_url": f"https://apps.apple.com/us/app/id{r['track_id']}",
            "vendor": r.get("vendor") or "",
            "proposed_facility": _fac_label(r),
            "name_agreement": f"{ag:.2f}",
            "CONFIRM [y/n]": "", "CORRECTED_FACILITY_ID": "", "NOTES": "",
            "_sort": (1, ag),
        })

    rows.sort(key=lambda r: r["_sort"])
    for r in rows:
        r.pop("_sort", None)
    return rows


def export(path: str | Path) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = build_rows()
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        w.writerows(rows)
    by_src: dict[str, int] = {}
    for r in rows:
        by_src[r["source"]] = by_src.get(r["source"], 0) + 1
    return {"path": str(path), "rows": len(rows), "by_source": by_src}


def import_decisions(path: str | Path, dry_run: bool = False) -> dict:
    """Apply confirmations and corrections.

    'y'  -> keep/create the proposed link.
    'n'  -> DELETE the proposed link (a wrong link is worse than none) and record an
            'unsure' disposition so it does not silently reappear next run.
    A CORRECTED_FACILITY_ID always wins over CONFIRM.
    """
    path = Path(path)
    in_scope = {r["facility_id"] for r in db.query(
        f"SELECT facility_id FROM facility WHERE {config.FACILITY_WHERE}")}
    known = db.known_track_ids()

    add, remove, dispositions, errors = [], [], [], []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            conf = (row.get("CONFIRM [y/n]") or "").strip().lower()
            corrected = (row.get("CORRECTED_FACILITY_ID") or "").strip()
            if not conf and not corrected:
                continue
            try:
                tid = int(row["track_id"])
            except (KeyError, TypeError, ValueError):
                errors.append(f"line {i}: unreadable track_id")
                continue
            if tid not in known:
                errors.append(f"line {i}: track_id {tid} not in corpus")
                continue

            proposed = (row.get("proposed_facility") or "").split("|")[0].strip()
            prop_id = int(proposed) if proposed.isdigit() else None

            if corrected:
                if not corrected.isdigit() or int(corrected) not in in_scope:
                    errors.append(f"line {i}: CORRECTED_FACILITY_ID {corrected!r} "
                                  f"missing or out of scope")
                    continue
                if prop_id and int(corrected) != prop_id:
                    remove.append((tid, prop_id))
                add.append({"facility_id": int(corrected), "track_id": tid,
                            "match_confidence": 1.0, "match_method": "manual",
                            "match_status": "Verified - corrected"})
            elif conf in ("y", "yes"):
                if not prop_id:
                    errors.append(f"line {i}: confirmed but no proposed facility id")
                    continue
                add.append({"facility_id": prop_id, "track_id": tid,
                            "match_confidence": 1.0, "match_method": "manual",
                            "match_status": "Verified - confirmed"})
            elif conf in ("n", "no"):
                if prop_id:
                    remove.append((tid, prop_id))
                dispositions.append({"track_id": tid, "disposition": "unsure",
                                     "decided_by": "verify_sheet",
                                     "note": "rejected proposed facility"})
            else:
                errors.append(f"line {i}: CONFIRM {conf!r} is not y/n")

    if not dry_run:
        for tid, fid in remove:
            db.exec_sql(f"DELETE FROM ga_facility_app WHERE track_id = {int(tid)} "
                        f"AND facility_id = {int(fid)}")
        if add:
            db.upsert("ga_facility_app", add, on_conflict="facility_id,track_id")
        if dispositions:
            db.upsert("ga_app_disposition", dispositions, on_conflict="track_id")

    return {"confirmed_or_corrected": len(add), "links_removed": len(remove),
            "dispositions": len(dispositions), "errors": errors[:50],
            "error_count": len(errors), "dry_run": dry_run}
