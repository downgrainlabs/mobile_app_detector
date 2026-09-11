"""Answer 'who makes the app for <course>?' from what the pipeline actually found.

Deliberately distinguishes three different answers, because conflating them would be
misleading:
  * LINKED      -- we found the app and know the vendor.
  * UNLINKED    -- an app in the corpus looks like this course but was never matched
                   to the facility (this is the manual-review queue).
  * NOT FOUND   -- nothing in the corpus. NOT the same as 'this course has no app':
                   the sweep is incomplete, and four vendors ship no per-club app at all.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config, db, vendors

_STOP = {"golf", "club", "course", "resort", "country", "links", "the", "at", "and",
         "gc", "cc", "of", "on", "a"}


def _toks(name: str) -> list[str]:
    return [t for t in re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split()
            if t and t not in _STOP]


def _score(a: str, b: str) -> float:
    ta, tb = set(_toks(a)), set(_toks(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class Answer:
    query: str
    facility: dict | None = None
    facility_candidates: list[dict] = field(default_factory=list)
    apps: list[dict] = field(default_factory=list)
    unlinked_apps: list[dict] = field(default_factory=list)
    note: str = ""


def find_facility(name: str, city: str | None = None,
                  state: str | None = None) -> tuple[dict | None, list[dict]]:
    toks = _toks(name)
    if not toks:
        return None, []
    where = [config.FACILITY_WHERE]
    where.append("(" + " OR ".join(
        f"facility_name ILIKE {db.lit('%' + t + '%')}" for t in toks[:3]) + ")")
    if state:
        where.append(f"(state_code = {db.lit(state.upper())} OR state ILIKE {db.lit(state)})")
    if city:
        where.append(f"city ILIKE {db.lit('%' + city + '%')}")
    rows = db.query(f"""
        SELECT facility_id, facility_name, city, state_code, website_url, active_status
        FROM facility WHERE {' AND '.join(where)} LIMIT 60
    """)
    if not rows and (city or state):     # relax location if it over-constrained
        rows = db.query(f"""
            SELECT facility_id, facility_name, city, state_code, website_url, active_status
            FROM facility WHERE {config.FACILITY_WHERE} AND (
              {' OR '.join(f"facility_name ILIKE {db.lit('%' + t + '%')}" for t in toks[:3])}
            ) LIMIT 60
        """)
    ranked = sorted(rows, key=lambda r: -_score(name, r["facility_name"]))
    return (ranked[0] if ranked else None), ranked[:6]


def apps_for_facility(facility_id: int) -> list[dict]:
    return db.query(f"""
        SELECT a.track_id, a.track_name, a.bundle_id, a.seller_name, a.seller_url,
               a.copyright, fa.match_method, fa.match_confidence, fa.match_status,
               v.vendor, v.confidence, v.matched_fields, v.conflict, v.conflict_detail
        FROM ga_facility_app fa
        JOIN ga_app a ON a.track_id = fa.track_id
        LEFT JOIN ga_app_vendor v ON v.track_id = a.track_id
        WHERE fa.facility_id = {int(facility_id)}
    """)


def apps_by_name(name: str, limit: int = 6) -> list[dict]:
    toks = _toks(name)
    if not toks:
        return []
    clause = " OR ".join(f"a.track_name ILIKE {db.lit('%' + t + '%')}" for t in toks[:3])
    rows = db.query(f"""
        SELECT a.track_id, a.track_name, a.bundle_id, a.seller_name, a.seller_url,
               v.vendor, v.confidence,
               EXISTS (SELECT 1 FROM ga_facility_app f WHERE f.track_id = a.track_id) linked
        FROM ga_app a LEFT JOIN ga_app_vendor v ON v.track_id = a.track_id
        WHERE a.delisted_at IS NULL AND ({clause}) LIMIT 60
    """)
    return sorted(rows, key=lambda r: -_score(name, r["track_name"]))[:limit]


def ask(name: str, city: str | None = None, state: str | None = None) -> Answer:
    ans = Answer(query=", ".join(x for x in (name, city, state) if x))
    fac, cands = find_facility(name, city, state)
    ans.facility, ans.facility_candidates = fac, cands
    if fac:
        ans.apps = apps_for_facility(fac["facility_id"])
    if not ans.apps:
        ans.unlinked_apps = [r for r in apps_by_name(name) if not r["linked"]]
    return ans


def render(ans: Answer) -> str:
    reg = vendors.load()
    L = [f"QUERY: {ans.query}"]
    if not ans.facility:
        L.append("  facility: NOT FOUND in the facility table")
        L.append("  (checked Golf Course + Active/Pending, USA only)")
    else:
        f = ans.facility
        L.append(f"  facility: #{f['facility_id']} {f['facility_name']} "
                 f"({f['city']}, {f['state_code']}) [{f['active_status']}]")
        if f.get("website_url"):
            L.append(f"            {f['website_url']}")

    if ans.apps:
        for a in ans.apps:
            v = a.get("vendor")
            disp = reg.vendors[v].display_name if v in reg.vendors else (v or "UNKNOWN")
            L.append("")
            L.append(f"  >>> VENDOR: {disp}   (confidence: {a.get('confidence')})")
            L.append(f"      app      : {a['track_name']}  [id {a['track_id']}]")
            L.append(f"      bundleId : {a.get('bundle_id')}")
            if a.get("seller_name"):
                L.append(f"      seller   : {a['seller_name']}")
            if a.get("copyright"):
                L.append(f"      copyright: {a['copyright']}")
            fields = [m["field"] for m in (a.get("matched_fields") or [])]
            if fields:
                L.append(f"      evidence : matched on {', '.join(fields)}")
            L.append(f"      linked by: {a.get('match_method')} "
                     f"({a.get('match_status')})")
            if a.get("conflict"):
                rivals = ", ".join(r["vendor"] for r in (a.get("conflict_detail") or []))
                L.append(f"      ** this listing also names: {rivals} "
                         f"(possible vendor switch)")
    elif ans.unlinked_apps:
        L.append("")
        L.append("  No app is LINKED to this facility, but the corpus has apps with a"
                 " similar name that were never matched (i.e. review queue):")
        for a in ans.unlinked_apps:
            L.append(f"      {a['track_name'][:38]:40s} vendor={str(a.get('vendor')):14s}"
                     f" bundle={a.get('bundle_id')}")
    else:
        L.append("")
        L.append("  No app found in the corpus.")
        L.append("  Caveat: that is not proof there is no app. The sweep is incomplete,")
        L.append("  and ForeTees / Buz Club / Whoosh / Club Prophet ship one shared app")
        L.append("  with no per-club listing to find.")

    if ans.facility_candidates and len(ans.facility_candidates) > 1 and not ans.apps:
        L.append("")
        L.append("  other facilities matching that name:")
        for c in ans.facility_candidates[1:5]:
            L.append(f"      #{c['facility_id']:6d} {c['facility_name'][:34]:36s} "
                     f"{c['city']}, {c['state_code']}")
    return "\n".join(L)
