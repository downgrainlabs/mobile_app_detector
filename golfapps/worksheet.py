"""Export/import the manual review worksheet.

Design constraints, learned the hard way earlier in this project:

  * Key on IDs, never names. A decision recorded against a name silently evaporates
    when the name changes -- the same failure mode as the manual merge rules.
  * Carry enough context to decide WITHOUT opening the App Store. The match service's
    candidates contain no city or state, so two rows reading "Deer Run Golf Course"
    are indistinguishable; every candidate here is enriched from `facility`.
  * Record negative decisions as first-class. Without them, every monthly run
    re-presents the same PGA TOUR and UK apps forever.
  * Fail loudly on import. A decision pointing at a track_id or facility_id that no
    longer exists is reported, not skipped.

Two link targets, because one app does not always mean one facility:
  * FACILITY_IDS -- one or more facility ids (comma-separated for multi-course apps
    like Yarmouth Golf, which covers Bass River and Bayberry Hills).
  * OWNER_ID     -- when the app belongs to a management group or municipality rather
    than a single course.
"""
from __future__ import annotations

import csv
import logging
import re
from pathlib import Path

from . import config, db, triage, vendors

log = logging.getLogger(__name__)

DECISIONS = ["facility", "owner", "not_a_facility_app", "out_of_scope_geo",
             "no_facility_in_db", "unsure"]

HEADER = [
    "track_id", "priority", "app_name", "vendor", "confidence",
    "bundle_id", "seller_name", "app_store_url",
    "seller_url", "support_url", "privacy_policy_url", "description",
    "candidate_1", "candidate_2", "candidate_3", "owner_suggestion",
    "SUGGESTED_DECISION",
    f"DECISION [{('|').join(DECISIONS)}]", "FACILITY_IDS", "OWNER_ID", "NOTES",
]


def _fmt_candidate(f: dict | None) -> str:
    if not f:
        return ""
    return (f"{f['facility_id']} | {f['facility_name']} | "
            f"{f.get('city') or '?'}, {f.get('state_code') or '?'} | "
            f"{f.get('active_status') or '?'}")


def _owner_index() -> dict[str, dict]:
    idx = {}
    for r in db.query("SELECT owner_id, owner_name FROM owner_entities"):
        n = re.sub(r"[^a-z0-9]+", " ", (r["owner_name"] or "").lower()).strip()
        if n:
            idx.setdefault(n, r)
    return idx


def _suggest_owner(seller: str | None, idx: dict[str, dict]) -> str:
    """Sellers are often the management group or town that owns the courses."""
    if not seller:
        return ""
    n = re.sub(r"[^a-z0-9]+", " ", seller.lower()).strip()
    n = re.sub(r"\b(llc|inc|lp|llp|ltd|corp|corporation|co|the)\b", "", n).strip()
    n = re.sub(r"\s+", " ", n)
    if not n:
        return ""
    if n in idx:
        r = idx[n]
        return f"{r['owner_id']} | {r['owner_name']}"
    toks = set(n.split())
    best, score = None, 0.0
    for key, r in idx.items():
        kt = set(key.split())
        if not kt:
            continue
        j = len(toks & kt) / len(toks | kt)
        if j > score:
            best, score = r, j
    return f"{best['owner_id']} | {best['owner_name']} (~{score:.0%})" if best and score >= 0.5 else ""


def build_rows() -> list[dict]:
    reg = vendors.load()

    apps = db.query("""
        SELECT a.track_id, a.track_name, a.bundle_id, a.seller_name, a.seller_url,
               a.support_url, a.privacy_policy_url, a.description,
               v.vendor, v.confidence
        FROM ga_app a
        LEFT JOIN ga_app_vendor v ON v.track_id = a.track_id
        WHERE a.delisted_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM ga_facility_app f WHERE f.track_id = a.track_id)
          AND NOT EXISTS (SELECT 1 FROM ga_app_disposition d WHERE d.track_id = a.track_id)
    """)

    amb = {r["track_id"]: r for r in db.query(
        "SELECT track_id, candidates FROM ga_match_ambiguous")}

    fac_ids = sorted({c["facility_id"] for r in amb.values()
                      for c in (r["candidates"] or []) if c.get("facility_id")})
    fac: dict[int, dict] = {}
    for i in range(0, len(fac_ids), 400):
        chunk = ",".join(str(x) for x in fac_ids[i:i + 400])
        for f in db.query(
            "SELECT facility_id, facility_name, city, state_code, active_status, "
            f"facility_type FROM facility WHERE facility_id IN ({chunk})"
        ):
            fac[f["facility_id"]] = f

    owners = _owner_index()
    in_scope_types = {"Golf Course"}
    rows = []
    for a in apps:
        # Only apps that plausibly belong to a facility -- otherwise the worksheet is
        # mostly consumer apps and nobody will finish it.
        if not (a.get("vendor") or vendors.looks_like_course_app(a)):
            continue

        cands = []
        for c in (amb.get(a["track_id"], {}).get("candidates") or []):
            f = fac.get(c.get("facility_id"))
            if not f:
                continue
            # Surface in-scope candidates first; keep others but rank them lower.
            cands.append((0 if (f.get("facility_type") in in_scope_types
                                and f.get("active_status") in ("Active", "Pending Opening"))
                          else 1, f))
        cands.sort(key=lambda t: t[0])
        top = [f for _, f in cands][:3]

        # Pre-classify so the reviewer confirms rather than researches. Consumer apps
        # and non-US courses are the bulk of the residue and are obvious at a glance;
        # leaving them blank makes someone re-derive that 300 times.
        blob = f"{a.get('track_name') or ''} {(a.get('description') or '')[:600]}"
        if not a.get("vendor") and triage.NOT_FACILITY_PATTERNS.search(blob):
            suggested = "not_a_facility_app"
        elif triage.NON_US_PATTERNS.search(blob):
            suggested = "out_of_scope_geo"
        elif triage.MULTI_COURSE_PATTERNS.search(blob):
            suggested = "facility"          # expect several ids in FACILITY_IDS
        else:
            suggested = ""

        # Candidates are only shown where they could plausibly be right. For a
        # consumer app the service still returns 3 high-confidence names
        # ("Topgolf Myrtle Beach" at 0.92 for "Myrtle Beach Golf") and offering those
        # invites a wrong pick.
        if suggested in ("not_a_facility_app", "out_of_scope_geo"):
            top = []

        vendor_disp = (reg.vendors[a["vendor"]].display_name
                       if a.get("vendor") in reg.vendors else (a.get("vendor") or ""))
        # Vendor-known with candidates is the fastest, highest-value work.
        priority = 1 if (a.get("vendor") and top) else 2 if a.get("vendor") else 3

        rows.append({
            "track_id": a["track_id"],
            "priority": priority,
            "app_name": a["track_name"],
            "vendor": vendor_disp,
            "confidence": a.get("confidence") or "",
            "bundle_id": a.get("bundle_id") or "",
            "seller_name": a.get("seller_name") or "",
            "app_store_url": f"https://apps.apple.com/us/app/id{a['track_id']}",
            "seller_url": a.get("seller_url") or "",
            "support_url": a.get("support_url") or "",
            "privacy_policy_url": a.get("privacy_policy_url") or "",
            "description": re.sub(r"\s+", " ", (a.get("description") or ""))[:220],
            "candidate_1": _fmt_candidate(top[0] if len(top) > 0 else None),
            "candidate_2": _fmt_candidate(top[1] if len(top) > 1 else None),
            "candidate_3": _fmt_candidate(top[2] if len(top) > 2 else None),
            "owner_suggestion": _suggest_owner(a.get("seller_name"), owners),
            "SUGGESTED_DECISION": suggested,
            f"DECISION [{('|').join(DECISIONS)}]": "",
            "FACILITY_IDS": "",
            "OWNER_ID": "",
            "NOTES": "",
        })

    rows.sort(key=lambda r: (r["priority"], r["vendor"], r["app_name"] or ""))
    return rows


# The minimal worksheet: app name, a link to look at it, and somewhere to put the
# answer. track_id stays because it is the key the import reads back -- names are not
# stable enough to round-trip on.
SLIM_HEADER = ["track_id", "app_name", "app_store_url", "FACILITY_ID", "OWNER_ID"]


def export(path: str | Path, slim: bool = False) -> dict:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = build_rows()
    by_pri: dict[int, int] = {}
    for r in rows:
        by_pri[r["priority"]] = by_pri.get(r["priority"], 0) + 1
    header = SLIM_HEADER if slim else HEADER
    if slim:
        rows = [{"track_id": r["track_id"], "app_name": r["app_name"],
                 "app_store_url": r["app_store_url"],
                 "FACILITY_ID": "", "OWNER_ID": ""} for r in rows]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(rows)
    return {"path": str(path), "rows": len(rows), "by_priority": by_pri}


def import_decisions(path: str | Path, dry_run: bool = False) -> dict:
    """Read a filled worksheet back. Loud about anything that does not resolve."""
    path = Path(path)
    decision_col = f"DECISION [{('|').join(DECISIONS)}]"
    known_apps = db.known_track_ids()
    in_scope = {r["facility_id"] for r in db.query(
        f"SELECT facility_id FROM facility WHERE {config.FACILITY_WHERE}")}
    known_owners = {r["owner_id"] for r in db.query(
        "SELECT owner_id FROM owner_entities")}

    links, dispositions, errors = [], [], []
    with path.open(encoding="utf-8-sig", newline="") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            dec = (row.get(decision_col) or "").strip().lower()
            fac_raw = (row.get("FACILITY_IDS") or row.get("FACILITY_ID") or "").strip()
            own_raw = (row.get("OWNER_ID") or "").strip()
            # Slim sheet has no DECISION column -- infer it from what was filled in.
            if not dec:
                if fac_raw:
                    dec = "facility"
                elif own_raw:
                    dec = "owner"
                else:
                    continue
            try:
                tid = int(row["track_id"])
            except (KeyError, TypeError, ValueError):
                errors.append(f"line {i}: unreadable track_id")
                continue
            if tid not in known_apps:
                errors.append(f"line {i}: track_id {tid} is not in the corpus")
                continue
            if dec not in DECISIONS:
                errors.append(f"line {i}: decision {dec!r} not one of {DECISIONS}")
                continue

            if dec == "facility":
                ids = [x.strip() for x in fac_raw.split(",") if x.strip()]
                if not ids:
                    errors.append(f"line {i}: decision 'facility' with no FACILITY_IDS")
                    continue
                for raw in ids:
                    try:
                        fid = int(raw)
                    except ValueError:
                        errors.append(f"line {i}: FACILITY_IDS {raw!r} is not a number")
                        continue
                    if fid not in in_scope:
                        errors.append(
                            f"line {i}: facility {fid} is not in scope "
                            f"(Golf Course + Active/Pending, USA)")
                        continue
                    links.append({"facility_id": fid, "track_id": tid,
                                  "match_confidence": 1.0, "match_method": "manual",
                                  "match_status": "Manual review"})
            elif dec == "owner":
                raw = own_raw
                if not raw.isdigit() or int(raw) not in known_owners:
                    errors.append(f"line {i}: OWNER_ID {raw!r} missing or unknown")
                    continue

            dispositions.append({
                "track_id": tid, "disposition": dec,
                "owner_id": int(own_raw) if own_raw.isdigit() else None,
                "decided_by": "worksheet",
                "note": (row.get("NOTES") or "").strip() or None,
            })

    if not dry_run:
        if links:
            db.upsert("ga_facility_app", links, on_conflict="facility_id,track_id")
        if dispositions:
            db.upsert("ga_app_disposition", dispositions, on_conflict="track_id")

    return {"decisions_read": len(dispositions), "links_created": len(links),
            "errors": errors[:50], "error_count": len(errors), "dry_run": dry_run}
