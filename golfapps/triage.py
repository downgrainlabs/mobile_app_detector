"""Bucket the unmatched residue so the manual queue can be sized honestly.

"Unresolved" is not one thing. Most of what fails to match is not review work at
all -- it is consumer apps and non-US courses that should be excluded once and never
surfaced again. Only genuinely ambiguous and multi-course cases need a person.

Every bucket here is a PROPOSAL. Nothing is written as a decision; `golfapps triage`
reports counts and samples, and dispositions are only persisted when a human accepts
them. The auto-classifiers are deliberately conservative -- anything uncertain lands
in needs_review rather than being quietly excluded.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field

from . import db, vendors

log = logging.getLogger(__name__)

# Consumer apps, tours, aggregators and GPS tools. These are not facility apps and
# never will be -- but only when nothing else suggests a specific course.
NOT_FACILITY_PATTERNS = re.compile(
    r"(?i)\b(pga tour|dp world tour|lpga|european tour|the masters|ryder cup|"
    r"golfnow|teeoff|supreme golf|golf ?now|leading courses|golfpass|"
    r"gps range ?finder|rangefinder|golf gps|scorecard app|handicap|ghin|"
    r"golf ?digest|golf channel|fantasy golf|golf betting|swing analy|launch monitor|"
    r"golf tips|golf lessons?|golf training|golf simulator|tee time alert|"
    r"tee times? (finder|search|deals)|book tee times anywhere|golf card|"
    r"golf ?pass|discount golf|golf deals)\b")

# Non-US signals. Deliberately narrow: a US course can mention 'Ontario, California'.
NON_US_PATTERNS = re.compile(
    r"(?i)\b(united kingdom|england|scotland|wales|ireland|northern ireland|"
    r"australia|new zealand|canada|ontario, canada|british columbia|alberta|"
    r"quebec|nova scotia|saskatchewan|manitoba|south africa|singapore|malaysia|"
    r"thailand|vietnam|philippines|japan|korea|china|dubai|abu dhabi|qatar|"
    r"spain|portugal|france|germany|italy|sweden|norway|denmark|finland|"
    r"netherlands|belgium|austria|switzerland|mexico|brazil|argentina|chile|"
    r"costa rica|bahamas|bermuda|jamaica|barbados|st\.? lucia|caribbean|"
    r"yorkshire|surrey|kent|essex|sussex|hampshire|lancashire|cheshire)\b")

MULTI_COURSE_PATTERNS = re.compile(
    r"(?i)(golf courses\b|our courses|all of our|multiple courses|"
    r"\b(two|three|four|five|both) (championship )?courses\b|golf trail|"
    r"resort courses|family of courses)")


@dataclass
class Triage:
    corpus_total: int = 0
    course_app_candidates: int = 0
    linked_total: int = 0
    linked_by_name: int = 0
    linked_by_domain: int = 0
    buckets: dict = field(default_factory=lambda: defaultdict(list))

    def counts(self) -> dict:
        return {k: len(v) for k, v in sorted(self.buckets.items(),
                                             key=lambda kv: -len(kv[1]))}


def _is_course_app(r: dict) -> bool:
    """Does this look like an app FOR a specific facility?"""
    if r.get("vendor"):
        return True
    return vendors.looks_like_course_app(r)


def run() -> Triage:
    t = Triage()

    t.corpus_total = db.query(
        "SELECT count(*) n FROM ga_app WHERE delisted_at IS NULL")[0]["n"]

    linked = db.query("""
        SELECT track_id, match_method, count(*) n
        FROM ga_facility_app GROUP BY 1, 2
    """)
    by_method = defaultdict(set)
    for r in linked:
        by_method[r["match_method"]].add(r["track_id"])
    t.linked_by_name = len(by_method.get("match_service", set()))
    t.linked_by_domain = len(by_method.get("domain", set()))
    linked_ids = set().union(*by_method.values()) if by_method else set()
    t.linked_total = len(linked_ids)

    rows = db.query("""
        SELECT a.track_id, a.track_name, a.description, a.bundle_id, a.seller_url,
               a.support_url, a.privacy_policy_url, v.vendor, v.confidence
        FROM ga_app a
        LEFT JOIN ga_app_vendor v ON v.track_id = a.track_id
        WHERE a.delisted_at IS NULL
    """)

    multi_linked = {r["track_id"] for r in db.query("""
        SELECT track_id FROM ga_facility_app GROUP BY 1 HAVING count(*) > 1
    """)}

    for r in rows:
        if not _is_course_app(r):
            continue
        t.course_app_candidates += 1
        if r["track_id"] in linked_ids:
            if r["track_id"] in multi_linked:
                t.buckets["linked_multi_course"].append(r)
            continue

        blob = f"{r.get('track_name') or ''} {(r.get('description') or '')[:600]}"

        # A vendor-labelled app IS a facility app -- never exclude it as consumer.
        if not r.get("vendor") and NOT_FACILITY_PATTERNS.search(blob):
            t.buckets["not_a_facility_app"].append(r)
        elif NON_US_PATTERNS.search(blob):
            t.buckets["out_of_scope_geo"].append(r)
        elif MULTI_COURSE_PATTERNS.search(blob):
            t.buckets["multi_course_needs_manual_links"].append(r)
        elif r.get("vendor"):
            # Built by a known vendor, so a real course exists somewhere -- this is
            # the genuine review queue.
            t.buckets["needs_review_vendor_known"].append(r)
        else:
            t.buckets["needs_review_unclassified"].append(r)

    return t


def render(t: Triage, samples: int = 6) -> str:
    counts = t.counts()
    review = (counts.get("needs_review_vendor_known", 0)
              + counts.get("needs_review_unclassified", 0)
              + counts.get("multi_course_needs_manual_links", 0))
    auto_excl = (counts.get("not_a_facility_app", 0)
                 + counts.get("out_of_scope_geo", 0))

    lines = [
        "=" * 74,
        "TRIAGE -- sizing the manual review queue",
        "=" * 74,
        "",
        f"  corpus (live apps)            : {t.corpus_total:,}",
        f"  look like facility apps       : {t.course_app_candidates:,}",
        f"  linked to a facility          : {t.linked_total:,}"
        f"   (name {t.linked_by_name:,} + domain {t.linked_by_domain:,})",
        "",
        "  RESIDUE BY BUCKET",
    ]
    for k, n in counts.items():
        lines.append(f"    {k:36s} {n:>6,}")
    lines += [
        "",
        f"  Auto-excludable (decide once, never re-surface) : {auto_excl:,}",
        f"  ACTUAL MANUAL REVIEW QUEUE                      : {review:,}",
        "",
        "  Note: auto-exclusion buckets are PROPOSALS, not decisions. Nothing is",
        "  written until accepted. Vendor-labelled apps are never auto-excluded as",
        "  consumer apps, since a known vendor implies a real course exists.",
        "",
    ]
    for k in ("needs_review_vendor_known", "multi_course_needs_manual_links",
              "needs_review_unclassified", "not_a_facility_app", "out_of_scope_geo"):
        rows = t.buckets.get(k, [])
        if not rows:
            continue
        lines.append(f"  --- {k} (showing {min(samples, len(rows))} of {len(rows)}) ---")
        for r in rows[:samples]:
            lines.append(f"    {str(r['track_name'])[:38]:40s} vendor={str(r['vendor']):14s}")
        lines.append("")
    return "\n".join(lines)
