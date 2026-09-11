"""Channel 2 -- match apps to facilities by web domain.

Completely independent of name matching, which is the point: it resolves apps whose
names are hopeless ("Crystal Caddy") and confirms the ones that aren't.

The signal: operator-published apps usually carry the COURSE's own website rather
than the vendor's in seller_url / support_url / privacy_policy_url. Cross-referenced
against facility.website_url, that is a near-identity.

Two things to know about the data:
  * `facility.domain` is NULL for all 21,250 rows -- a latent gap. The host has to be
    parsed out of `facility.website_url` (populated for 16,282).
  * The match inherits facility data errors. riobravocountryclub.com resolves to a
    facility named "Scarlet and Gray Golf Club", which is a bad website_url on that
    row, not a bad domain rule. So domain hits that DISAGREE with the name match are
    surfaced for review rather than silently trusted.
"""
from __future__ import annotations

import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field

from . import config, db, vendors

log = logging.getLogger(__name__)

_HOST_RE = re.compile(r"https?://([^/]+)", re.I)

# Hosts that identify the VENDOR, not the course. Matching on these would link every
# Gallus app to whichever facility happens to list gallusgolf.com as its website.
GENERIC_HOSTS = {
    "apple.com", "itunes.apple.com", "apps.apple.com", "facebook.com", "twitter.com",
    "instagram.com", "google.com", "youtube.com", "linkedin.com", "wix.com",
    "squarespace.com", "godaddy.com", "wordpress.com", "golfnow.com", "teeitup.com",
    "clubcaddie.com", "foreupsoftware.com", "golftrac.com", "teesnap.com",
    # App-builder / agency / white-label platform domains -- found 2026-08-19 while
    # auditing the Phase 2 collision residue. These showed up as an app's ONLY
    # "host" and were being treated as club-specific evidence, when they're the
    # platform the app was built ON, same failure class as Hospitality App
    # Development / com.guestexpressapp (see vendors.yaml jonas note).
    "nbcsportsmobileapps.com", "dynamicsgolf.com", "apps.dynamicsgolf.com",
    "appbuild.io", "cms.appbuild.io", "imobileapp.com", "talgrace.com",
    # Shared-hosting / management-company / chain domains -- found 2026-08-19
    # while auditing "Looks Good" for facilities receiving >1 domain-method link.
    # github.io: a static-site host (GitHub Pages); registrable() has no concept
    # of it being a shared apex like wix.com, so *.github.io apps were colliding
    # on the bare "github.io" registrable form -- 11 unrelated apps (most not even
    # golf apps) got linked to Bemus Point Golf Course, which merely happened to
    # be the only facility in the table with a github.io site.
    # kempersports.com / hyatt.com: golf-management-company and hotel-chain sites
    # that ONE managed property's website_url happens to be -- any other app
    # published under the same management company's generic domain then looked
    # like a match to that one specific property (The Dunes Club claimed
    # Riverwalk GC, Mojave Golf, etc.; Wild Dunes claimed generic Hyatt apps).
    "github.io", "kempersports.com", "hyatt.com",
}


def host(url: str | None) -> str | None:
    if not url:
        return None
    m = _HOST_RE.match(url.strip())
    h = (m.group(1) if m else url.strip()).lower()
    h = h.split(":")[0]
    if h.startswith("www."):
        h = h[4:]
    return h or None


# Multi-part public suffixes we actually encounter. Not exhaustive -- a full PSL
# would be overkill for a US-scoped pipeline.
_MULTI_SUFFIX = {"co.uk", "org.uk", "com.au", "co.nz", "co.za", "com.br",
                 "co.jp", "ne.jp", "or.jp", "com.mx", "co.in",
                 # Canadian provincial second-level domains -- without these,
                 # "epgcc.ab.ca" collapses to the bare "ab.ca", which every
                 # other Alberta club's domain ALSO collapses to, creating the
                 # same false-collision-magnet bug as an unrecognised shared
                 # host (found 2026-08-19 running the Canada_reference.csv
                 # domain sweep -- "ab.ca" briefly looked like it matched 3
                 # different real clubs at once).
                 "ab.ca", "bc.ca", "mb.ca", "nb.ca", "nf.ca", "nl.ca", "ns.ca",
                 "nt.ca", "nu.ca", "on.ca", "pe.ca", "qc.ca", "sk.ca", "yk.ca"}


def registrable(h: str | None) -> str | None:
    """eTLD+1. Clubs put their app's support page on subdomains
    (members.governorsclubnc.com) while facility.website_url stores the apex, so
    exact-host matching misses them. Worth +20 links, measured."""
    if not h:
        return None
    parts = h.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_SUFFIX:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def load_canada_hosts(path: str = None) -> dict[str, list[dict]]:
    """Index of every Canadian course's own website, from Derek's reference
    export (2026-08-19) -- 2205 courses with output.website_url. Checking an
    app's domain against this BEFORE match-service/facility lookup catches
    Canadian courses early: they routinely have a US-lookalike name
    ("Westwood Country Club", "Highlands Golf Club") that match-service or a
    facility-name search will happily resolve to the wrong US course, and no
    amount of tuning the US-side matcher fixes a country-of-origin problem."""
    import csv as _csv
    if path is None:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "Canada_reference.csv")
    idx: dict[str, list[dict]] = defaultdict(list)
    with open(path, encoding="utf-8-sig") as f:
        for row in _csv.DictReader(f):
            url = row.get("output.website_url")
            h = host(url)
            if not h:
                continue
            info = {"course_name": row.get("Manual Facility Name") or row.get("input.course_name"),
                   "city": row.get("input.city"), "province": row.get("State Abbr.")}
            idx[h].append(info)
            reg = registrable(h)
            if reg and reg != h:
                idx[reg].append(info)
    return idx


def vendor_hosts() -> set[str]:
    """Every domain any vendor claims -- these name the builder, not the club."""
    out = set(GENERIC_HOSTS)
    for v in vendors.load().vendors.values():
        for d in v.domains:
            out.add(d.lower())
    return out


@dataclass
class DomainStats:
    apps_with_candidate_host: int = 0
    distinct_hosts: int = 0
    hosts_matching_a_facility: int = 0
    unambiguous_links: int = 0
    ambiguous_hosts: int = 0
    agreed_with_name_match: int = 0
    disagreed_with_name_match: int = 0
    new_links: int = 0
    conflicts: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["conflicts"] = self.conflicts[:40]
        return d


def app_hosts() -> dict[int, set[str]]:
    """Per app, the non-vendor hosts it points at."""
    skip = vendor_hosts()
    rows = db.query("""
        SELECT track_id, seller_url, support_url, privacy_policy_url, developer_website
        FROM ga_app
        WHERE delisted_at IS NULL
          AND (seller_url IS NOT NULL OR support_url IS NOT NULL
               OR privacy_policy_url IS NOT NULL OR developer_website IS NOT NULL)
    """)
    out: dict[int, set[str]] = {}
    for r in rows:
        hs = set()
        for f in ("seller_url", "support_url", "privacy_policy_url", "developer_website"):
            h = host(r.get(f))
            if not h:
                continue
            # Drop vendor and generic hosts, and any subdomain of them.
            if any(h == s or h.endswith("." + s) for s in skip):
                continue
            hs.add(h)
        if hs:
            out[r["track_id"]] = hs
    return out


def facility_hosts() -> dict[str, list[dict]]:
    rows = db.query(f"""
        SELECT facility_id, facility_name, city, state_code, website_url
        FROM facility
        WHERE website_url IS NOT NULL AND {config.FACILITY_WHERE}
    """)
    idx: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        h = host(r["website_url"])
        if not h:
            continue
        idx[h].append(r)
        reg = registrable(h)
        if reg and reg != h:
            idx[reg].append(r)
    return idx


def run(apply_links: bool = True) -> DomainStats:
    stats = DomainStats()
    apps = app_hosts()
    facs = facility_hosts()
    stats.apps_with_candidate_host = len(apps)
    stats.distinct_hosts = len({h for hs in apps.values() for h in hs})

    existing = defaultdict(set)
    for r in db.query("SELECT facility_id, track_id, match_method FROM ga_facility_app"):
        existing[r["track_id"]].add(r["facility_id"])

    links = []
    for track_id, hosts in apps.items():
        hits: list[dict] = []
        for h in hosts:
            hits.extend(facs.get(h, []))
            reg = registrable(h)
            if reg and reg != h:
                hits.extend(facs.get(reg, []))
        if not hits:
            continue
        stats.hosts_matching_a_facility += 1

        unique = {f["facility_id"]: f for f in hits}
        if len(unique) > 1:
            # Several facilities claim this domain -- often a management group with
            # one website across many courses. Not resolvable here.
            stats.ambiguous_hosts += 1
            continue

        fac = next(iter(unique.values()))
        stats.unambiguous_links += 1

        prior = existing.get(track_id)
        if prior:
            if fac["facility_id"] in prior:
                stats.agreed_with_name_match += 1
                continue
            # Two independent channels naming DIFFERENT facilities. One of them is
            # wrong and we cannot tell which from here -- record, do not overwrite.
            stats.disagreed_with_name_match += 1
            stats.conflicts.append({
                "track_id": track_id,
                "name_match_facility_ids": sorted(prior),
                "domain_match_facility_id": fac["facility_id"],
                "domain_match_name": fac["facility_name"],
                "hosts": sorted(hosts),
            })
            continue

        links.append({
            "facility_id": fac["facility_id"],
            "track_id": track_id,
            "match_confidence": 0.95,
            "match_method": "domain",
            "match_status": "Match - Website Domain",
        })

    stats.new_links = len(links)
    if apply_links and links:
        db.upsert("ga_facility_app", links, on_conflict="facility_id,track_id")
    log.info("domain match: %s", stats.as_dict())
    return stats
