"""Channel 4 -- identify a club from its app icon via SerpAPI's Google Lens engine.

The premise (Derek's) is the same as webdetect.py: don't ask an image "which facility
is this?", ask "where else does this image appear on the web?". What changed is the
PRODUCT. Cloud Vision WEB_DETECTION and Google Lens are different systems with
different indexes -- measured directly on the Juliette Falls icon, WEB_DETECTION
returned unrelated stock-photo sites while Lens returned the club's Chamber of
Commerce listing, address, phone number, and its own domain, with no cropping needed.

So this module does NOT reuse webdetect.py's crop-first approach. Sending the full
icon is deliberate: Lens's own comparison already resolved through the vendor's house
template on the one case tested, and cropping cost nothing but also gained nothing
there. Re-evaluate if a stratified sample shows template contamination.

Two things this channel adds that WEB_DETECTION could not:
  * organic_results / visual_matches snippets often contain a full street address,
    which resolves ambiguity that a bare domain cannot (which of six Walnut Creeks).
  * `ai_overview` exists but requires a SEPARATE paid call to resolve its page_token --
    skipped here to conserve the free-tier budget; visual_matches alone carried
    everything needed for Juliette Falls.

Budget discipline: SerpAPI free tier is 250 searches/month, not 1000/month like Cloud
Vision. Every call is cached in ga_lens_detection and NEVER re-issued. Callers MUST
pass an explicit limit; there is no unbounded default.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import requests

from . import db, domainmatch

log = logging.getLogger(__name__)

SEARCH_ENDPOINT = "https://serpapi.com/search"

# Same idea as webdetect.NEVER_A_CLUB: hosts that identify infrastructure, not a club.
#
# Found via a real failure (Derek, 2026-08-18): searching "Arrowhead Country Club"
# put discgolfscene.com (a disc-golf TOURNAMENT LISTING that happens to have been
# held at a venue of the same name) ahead of arrowheadcc.golf -- SerpAPI's own #1
# result and the club's actual homepage. The fix is two-fold: exclude directory/
# aggregator/social/real-estate sites that mention a club without BEING the club, and
# weight position far more steeply so Google's own top pick isn't out-voted by noise.
NEVER_A_CLUB = {
    "apple.com", "itunes.apple.com", "apps.apple.com", "play.google.com",
    "google.com", "gstatic.com", "facebook.com", "fb.com", "instagram.com",
    "twitter.com", "x.com", "linkedin.com", "youtube.com", "pinterest.com",
    "wikipedia.org", "amazon.com", "yelp.com", "tripadvisor.com",
    "golfnow.com", "golfpass.com", "golfdigest.com", "allsquaregolf.com",
    "bing.com", "yandex.com",
    # directory / aggregator / listing sites -- they describe a club, they aren't one
    "discgolfscene.com", "theorg.com", "echofineproperties.com",
    "directory.pga.org", "mapquest.com", "tiktok.com",
    "myrtlebeachgolfdirectors.com", "zillow.com", "realtor.com", "redfin.com",
    "bestneighborhood.org", "niche.com", "mapcarta.com", "cybo.com",
    "chamberofcommerce.com", "manta.com", "bbb.org", "dnb.com", "yellowpages.com",
    "foursquare.com", "opentable.com", "bark.com", "thumbtack.com",
    "golfcourse-reviews.com", "golflink.com", "worldgolf.com",
}

# Address pattern: "20500 E Pennsylvania Ave. Dunnellon, FL 34432" or
# "... Dunnellon, FL 34432, USA ...". Captures city + 2-letter code -- deliberately
# not US-only, since a Canadian province code ("Belleville ON") is exactly the signal
# that must be caught, not silently dropped as an unrecognized state.
#
# The comma is OPTIONAL: Facebook page-location titles read "Belleville ON" with no
# comma, and requiring one meant the actual Quinte/Ontario evidence at rank 1 was
# missed entirely -- the country still got caught (via a stray .co.uk TLD five ranks
# later), but on the wrong, less legible evidence. This regex is what lets the real
# city+province get recorded, per Derek: "pull in the city and province."
#
# The city group is bounded to 1-3 CAPITALIZED words -- letting it run through
# lowercase connector words ("Resort - Golf in Pevely, MO" captured city=
# "Resort - Golf in Pevely" instead of "Pevely") produced a garbled span that
# _NOT_A_CITY_TOKENS then correctly rejected -- but that threw out the real
# answer buried inside it too, discarding a call the site actually had.
_ADDRESS_RE = re.compile(
    r"\b((?:[A-Z][a-zA-Z'.\-]*\s+){0,2}[A-Z][a-zA-Z'.\-]*),?\s+([A-Z]{2})\b(?:\s*(\d{5}))?")

CANADIAN_PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE",
                      "QC", "SK", "YT"}
_US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
_NON_US_COUNTRY_RE = re.compile(
    r"(?i)\b(canada|ontario|british columbia|alberta|quebec|nova scotia|"
    r"saskatchewan|manitoba|united kingdom|england|scotland|wales|ireland|"
    r"australia|new zealand)\b")

# A "city" that is actually the club's own name/suffix ("Brook Hollow GC",
# "Ridgewood Country Club TX") must not be accepted as a real place, and the
# trailing 2 letters of an abbreviated club name ("GC", "CC") must not be
# accepted as a state code just because they're capitalized and 2 letters --
# both produced false extractions (Brook Hollow -> "GC" read as a state,
# 2026-08-19). resolve_ordered() already guarded both; extract_city_state()
# -- the function run_with_matcher() actually uses -- never got the same
# fix ported over.
_NOT_A_CITY_TOKENS = {"golf", "club", "course", "country", "resort", "links",
                     "the", "at", "life", "social", "dining", "home"}
_NON_US_TLDS = (".ca", ".co.uk", ".uk", ".com.au", ".co.nz", ".ie")


def api_key() -> str:
    import os
    k = os.getenv("SERPAPI_KEY")
    if not k:
        raise RuntimeError("Set SERPAPI_KEY in the environment.")
    return k


@dataclass
class LensStats:
    considered: int = 0
    cached: int = 0
    called: int = 0
    api_errors: int = 0
    with_candidate_hosts: int = 0
    with_city_state: int = 0
    resolved_to_facility: int = 0
    new_links: int = 0
    credits_remaining: int | None = None
    samples: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["samples"] = self.samples[:40]
        return d


def lens_search(image_url: str, key: str, query: str | None = None) -> dict:
    """`query` runs alongside the image via SerpAPI's `q` param (google_lens supports
    this for type=all/visual_matches/products). Derek's point: a bare icon is
    ambiguous -- a golf-ball template or a monogram matches whatever else looks like
    it. Anchoring the search with the app's own name is what let the first raw-image
    test collide 4 unrelated apps onto fairmontmontana.com; passing the name changes
    what Lens treats as relevant."""
    params = {"engine": "google_lens", "url": image_url, "api_key": key}
    if query:
        params["q"] = query
    r = requests.get(SEARCH_ENDPOINT, params=params, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"SerpAPI {r.status_code}: {r.text[:300]}")
    d = r.json()
    if "error" in d:
        raise RuntimeError(f"SerpAPI error: {d['error']}")
    return d


def candidate_hosts(resp: dict) -> list[tuple[str, int]]:
    """Legacy aggregate ranking -- kept only for the calibration CSV's display column.
    Do NOT use this to pick THE answer: aggregating scores let a directory/tournament
    site that shows up in both visual_matches and organic_results out-score the
    single most relevant (position-1) result. See resolve_ordered() for the fix."""
    skip = domainmatch.vendor_hosts() | NEVER_A_CLUB
    counts: dict[str, int] = {}
    for item in (resp.get("visual_matches") or []):
        h = domainmatch.host(item.get("link"))
        if not h or any(h == s or h.endswith("." + s) for s in skip):
            continue
        pos = item.get("position") or 50
        counts[h] = counts.get(h, 0) + max(1, 30 - pos)
    for item in (resp.get("organic_results") or []):
        h = domainmatch.host(item.get("link"))
        if not h or any(h == s or h.endswith("." + s) for s in skip):
            continue
        counts[h] = counts.get(h, 0) + 5
    return sorted(counts.items(), key=lambda kv: -kv[1])


def resolve_ordered(resp: dict, facs: dict) -> dict:
    """Walk visual_matches, then organic_results, in Google's own relevance order --
    never re-sorted, never aggregated. Extract a domain candidate and a city/state/
    country as they're found, in position order.

    Two lessons baked in, both from real failures:

    1. (Arrowhead Country Club) The old scoring let discgolfscene.com -- a disc-golf
       tournament LISTING, present in both visual_matches and organic_results --
       outscore arrowheadcc.golf, SerpAPI's own #1 result and the club's real
       homepage. Blending scores across unrelated items is the bug; trusting
       position order is the fix.

    2. (Golf Courses of Quinte) Rank 1 said "Belleville ON" -- unambiguous Canada --
       but the old code didn't recognize "ON" as a state, silently dropped it, and
       kept walking until a same-named-coincidence US site showed up at rank 8 and
       got accepted as a "match". Per Derek: once the country is confidently known,
       do not waver from it. The fix does NOT stop the walk (later items may still
       carry a better city/province for storage), it locks the country on first
       confident signal and, from then on, refuses to accept ANY domain hit that
       would imply a different country -- a coincidental same-named US site five
       ranks later must never override an established Canadian result.

    A locked non-US country is not a failure: our facility table is US-only, so the
    correct, EXPECTED outcome for a Canadian course is that it goes through the
    matcher and gets a clean No Match. The country/city/province are still recorded
    so a Canadian course slots in for free the moment Canadian facilities exist.
    """
    skip = domainmatch.vendor_hosts() | NEVER_A_CLUB
    # Every rank-ordered independent facility a domain hit points to. NOT collapsed
    # to "first wins": Ridgewood Country Club has a REAL Waco TX facility (rank 3)
    # and a REAL Danbury CT facility (rank 12, corroborated twice -- the Google Play
    # package name itself contains "ridgewoodcountryclubct", and the explicit title
    # says "Ridgewood Country Club CT"). Taking rank 3 as THE answer just because it
    # came first would silently prefer the weaker-evidenced facility. When distinct
    # domain hits disagree, that is a genuine name collision to flag, not resolve.
    domain_candidates: list[dict] = []
    seen_fids: set[int] = set()
    city_hit = None
    country = None            # locked on first confident signal; never overwritten
    country_evidence = None
    items = list(resp.get("visual_matches") or []) + list(resp.get("organic_results") or [])

    for rank, item in enumerate(items):
        h = domainmatch.host(item.get("link"))
        blob = f"{item.get('title') or ''} {item.get('snippet') or ''}"

        # Country signal: TLD, explicit country/province name, or a "City, XX" whose
        # XX is a Canadian province. Checked on every item, locked on the first hit.
        if country is None:
            if h and h.endswith(_NON_US_TLDS):
                country, country_evidence = "non-US", {"rank": rank, "via": f"tld:{h}"}
            elif _NON_US_COUNTRY_RE.search(blob):
                country = "non-US"
                country_evidence = {"rank": rank, "via": _NON_US_COUNTRY_RE.search(blob).group(0)}

        m = _ADDRESS_RE.search(blob)
        if m:
            city, code = m.group(1).strip(), m.group(2)
            valid_city = (len(city) >= 3
                         and not (set(city.lower().split()) & _NOT_A_CITY_TOKENS))
            if valid_city and code in CANADIAN_PROVINCES and country is None:
                country, country_evidence = "CA", {"rank": rank, "via": f"{city}, {code}"}
            elif valid_city and code in _US_STATE_CODES and country is None:
                country = "US"
            if valid_city and city_hit is None:
                city_hit = {"city": city, "state": code, "rank": rank,
                           "is_us_state": code in _US_STATE_CODES}

        # A domain hit is only ever accepted while the country is US or still
        # undetermined. Once locked non-US, a same-named US site is coincidence,
        # not confirmation -- exactly the Corry Country Club / Quinte failure.
        if h and country in (None, "US") and not any(h == s or h.endswith("." + s) for s in skip):
            m2 = facs.get(h) or facs.get(domainmatch.registrable(h))
            if m2 and len({x["facility_id"] for x in m2}) == 1:
                fid = m2[0]["facility_id"]
                if fid not in seen_fids:
                    seen_fids.add(fid)
                    domain_candidates.append({"host": h, "facility_id": fid,
                                              "facility_name": m2[0]["facility_name"],
                                              "rank": rank})

    domain_hit = domain_candidates[0] if len(domain_candidates) == 1 else None
    return {"domain": domain_hit, "domain_candidates": domain_candidates,
           "city_state": city_hit, "country": country,
           "country_evidence": country_evidence}


def extract_city_state(resp: dict) -> tuple[str | None, str | None]:
    """Pull a US city/state out of visual_matches + organic_results titles/snippets.

    First valid hit in RANK order wins -- NOT a majority vote. visual_matches
    is ordered by similarity to the query image, so rank 0 is the single most
    trustworthy signal available. Equal-weight voting let a same-named club
    elsewhere win on a technicality: Mystic Creek Golf Club's rank-0 result
    said "El Dorado, AR" (matching the app's own Arkansas-shaped icon), but
    two lower-ranked "Milford, MI" mentions outvoted it 2-to-1 (2026-08-20,
    caught by Derek noticing the icon). visual_matches is checked in full
    before falling back to organic_results, which are plain text search
    results with no visual confirmation at all.
    """
    for coll in ("visual_matches", "organic_results"):
        for item in (resp.get(coll) or []):
            blob = f"{item.get('title') or ''} {item.get('snippet') or ''}"
            for m in _ADDRESS_RE.finditer(blob):
                city, state = m.group(1).strip(), m.group(2)
                if len(city) < 3 or (set(city.lower().split()) & _NOT_A_CITY_TOKENS):
                    continue
                if state not in _US_STATE_CODES and state not in CANADIAN_PROVINCES:
                    continue
                return city, state
    return None, None


CALIBRATION_HEADER = [
    "track_id", "app_name", "app_store_url", "icon_url",
    "top_hosts", "proposed_facility", "extracted_city_state",
    "CORRECT [y/n]", "ACTUAL_FACILITY_ID", "NOTES",
]


def export_calibration(path: str) -> dict:
    """Every cached Lens result, for a human to grade before anything auto-links.

    Built from ga_lens_detection alone -- no new API calls. This exists because the
    first batch measured ~20% real precision against a naive domain-accept rule (name
    agreement was the giveaway: 4 unrelated apps all 'resolved' to the same Montana
    resort via fairmontmontana.com). Grading against ground truth here is what tells
    us whether a blocklist + name-overlap filter actually fixes it, rather than
    guessing and burning more of the 250/month budget on the same mistake.
    """
    import csv as _csv
    rows = db.query("""
        SELECT l.track_id, a.track_name, a.artwork_url, l.hosts, l.resolved_host,
               l.resolved_facility_id, l.extracted_city, l.extracted_state,
               f.facility_name, f.city, f.state_code
        FROM ga_lens_detection l
        JOIN ga_app a ON a.track_id = l.track_id
        LEFT JOIN facility f ON f.facility_id = l.resolved_facility_id
        ORDER BY (l.resolved_facility_id IS NULL), l.track_id
    """)
    out = []
    for r in rows:
        hosts = ", ".join(h.get("host", "") for h in (r["hosts"] or [])[:5])
        prop = (f"{r['resolved_facility_id']} | {r['facility_name']} | "
               f"{r['city']}, {r['state_code']}") if r["resolved_facility_id"] else ""
        cs = (f"{r['extracted_city'] or ''}, {r['extracted_state'] or ''}"
             if (r["extracted_city"] or r["extracted_state"]) else "")
        out.append({
            "track_id": r["track_id"], "app_name": r["track_name"],
            "app_store_url": f"https://apps.apple.com/us/app/id{r['track_id']}",
            "icon_url": r["artwork_url"] or "",
            "top_hosts": hosts, "proposed_facility": prop,
            "extracted_city_state": cs,
            "CORRECT [y/n]": "", "ACTUAL_FACILITY_ID": "", "NOTES": "",
        })
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=CALIBRATION_HEADER)
        w.writeheader()
        w.writerows(out)
    return {"path": path, "rows": len(out),
            "with_proposed_facility": sum(1 for r in out if r["proposed_facility"])}


@dataclass
class PipelineStats:
    """Two-stage pipeline: Lens (image + app-name query) extracts city/state/domain,
    then match-service-2-0 -- the SAME validated matcher Phase 2 uses -- decides
    whether a link exists. A raw domain string is never accepted on its own; if the
    matcher says No Match, there is no link, full stop.
    """
    considered: int = 0
    called: int = 0
    api_errors: int = 0
    artwork_backfilled: int = 0
    non_us_skipped: int = 0
    no_golf_signal_skipped: int = 0
    with_city_state: int = 0
    submitted_to_matcher: int = 0
    matched: int = 0
    no_match: int = 0
    rejected_out_of_scope: int = 0
    new_links: int = 0
    credits_remaining: int | None = None
    samples: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["samples"] = self.samples[:60]
        return d


_CA_PROVINCES = {
    "british columbia": "BC", "ontario": "ON", "nova scotia": "NS",
    "alberta": "AB", "new brunswick": "NB", "saskatchewan": "SK",
    "quebec": "QC", "manitoba": "MB", "prince edward island": "PE",
    "newfoundland and labrador": "NL", "newfoundland": "NL", "yukon": "YT",
    "northwest territories": "NT", "nunavut": "NU",
}
_OTHER_COUNTRIES_RE = re.compile(
    r"(?i)\b(england|scotland|wales|dublin|cork|cape town|abu dhabi|dubai|"
    r"doha|kigali|muscat|mauritius|australia)\b")
_US_SAFE = ("saipan", "mariana islands")


def likely_non_us(a: dict) -> str | None:
    """Cheap, un-stored check -- recompute from data already on hand (cached
    subtitle, the app's own URL fields) every time, rather than persisting a
    per-app exclusion decision. The normal match-service path already
    naturally discards a Canadian course on its own (no state code, no
    candidate in a US-only facility table); this check exists ONLY to keep
    Lens from wasting a credit AND a wrong guess on one -- Lens does its own
    independent image-based city/state extraction, which can hallucinate a
    plausible-looking US location (a Red Deer, Alberta club got extracted as
    Sugar Land, TX and false-matched a real Texas facility, 2026-08-19)."""
    subtitle = (a.get("subtitle") or "").lower()
    if subtitle and not any(s in subtitle for s in _US_SAFE):
        if "canada" in subtitle:
            return "Canada"
        for name, code in _CA_PROVINCES.items():
            if re.search(r"\b" + re.escape(name) + r"\b", subtitle) or \
               re.search(r",\s*" + code.lower() + r"\b", subtitle):
                return f"Canada ({code})"
        m = _OTHER_COUNTRIES_RE.search(subtitle)
        if m:
            return m.group(1).title()

    skip = domainmatch.vendor_hosts()
    canada_hosts = domainmatch.load_canada_hosts()
    for field in ("seller_url", "support_url", "privacy_policy_url", "developer_website"):
        h = domainmatch.host(a.get(field))
        if not h or any(h == s or h.endswith("." + s) for s in skip):
            continue
        if h.endswith(".ca") or domainmatch.registrable(h) in canada_hosts:
            return "Canada (domain)"
    return None


def lacks_golf_signal(a: dict) -> bool:
    """These vendors (Chronogolf etc.) also serve plain social/country clubs
    with no golf course at all -- a name like 'Coronado Club' or 'The Madison
    Club' then collides with an unrelated real golf facility that happens to
    share the name, and Lens's image search is exactly the channel most
    exposed to that (a generic club-logo visual match carries no golf-
    specific signal to rule it out). Substring checks deliberately catch
    variants for free: "golfing" contains "golf", "tee times" contains
    "tee time".

    Originally scoped to gating the live Lens API call only (Derek,
    2026-08-20); broadened the same day to also gate the NEEDS_LENS/
    NEEDS_LINKING candidate pool itself via classify() below, so an app that
    will never be sent to Lens doesn't sit in review queues as if it might
    be. match-service's EXISTING links are still trusted as-is either way --
    this only ever decides whether an app is worth pursuing further, never
    un-does something already matched."""
    text = f"{a.get('track_name') or ''} {a.get('description') or ''}".lower()
    return not (("golf" in text) or ("tee time" in text))


def export_needs_lens(path: str = "docs/needs_lens.csv",
                      excluded_path: str = "docs/needs_lens_excluded.csv") -> dict:
    """The real, standing 'what's left before Lens' export -- every
    vendor-confirmed app with no facility link, filtered through classify()
    so the file only ever contains genuine candidates. Excluded rows go to a
    companion file with their reason, never silently dropped. This is the
    last step of the standard pre-Lens pipeline (join -> domainmatch ->
    subtitle_resolve -> screenshot_ocr -> this)."""
    import csv as _csv

    rows = db.query("""
        SELECT a.track_id, a.track_name, a.description, a.subtitle, v.vendor,
               a.seller_url, a.support_url, a.privacy_policy_url, a.developer_website
        FROM ga_app a
        JOIN ga_app_vendor v ON v.track_id = a.track_id AND v.vendor IS NOT NULL
        LEFT JOIN ga_facility_app fa ON fa.track_id = a.track_id
        LEFT JOIN app_crosswalk cw ON cw.track_id = a.track_id
        WHERE a.delisted_at IS NULL AND a.not_a_golf_course_at IS NULL
          AND a.canadian_course_at IS NULL AND fa.track_id IS NULL
          AND cw.track_id IS NULL
        ORDER BY v.vendor, a.track_name
    """)

    def urls(r):
        vals = [r.get(k) for k in ("seller_url", "support_url", "privacy_policy_url",
                                   "developer_website") if r.get(k)]
        return " | ".join(vals)

    needs_lens_rows, excluded = [], []
    for r in rows:
        reason = classify(r, r["vendor"])
        row = {
            "track_id": r["track_id"], "app_name": r["track_name"], "vendor": r["vendor"],
            "app_store_url": f"https://apps.apple.com/us/app/id{r['track_id']}",
            "app_urls": urls(r),
            "description_snippet": (r.get("description") or "")[:200].replace("\n", " "),
        }
        if reason:
            row["exclude_reason"] = reason
            excluded.append(row)
        else:
            row.update({"needs_lens": "", "not_a_golf_course": "", "see_notes": "", "NOTES": ""})
            needs_lens_rows.append(row)

    header = ["track_id", "app_name", "vendor", "app_store_url", "app_urls",
             "description_snippet", "needs_lens", "not_a_golf_course", "see_notes", "NOTES"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(needs_lens_rows)

    excluded_header = ["track_id", "app_name", "vendor", "app_store_url", "app_urls",
                       "description_snippet", "exclude_reason"]
    with open(excluded_path, "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=excluded_header)
        w.writeheader()
        w.writerows(excluded)

    return {"path": path, "needs_lens": len(needs_lens_rows),
           "excluded_path": excluded_path, "excluded": len(excluded)}


def export_collision_residue(path: str = "docs/collision_residue.csv") -> dict:
    """Apps that already have a link, but match-service flagged multiple
    same-confidence candidates for the underlying name/city/state query --
    a genuine collision a human needs to pick between, not something Lens
    would help with (Lens answers 'what club is this', not 'which of these
    2 known facilities'). Distinct from needs_lens: these have SOME
    evidence already; needs_lens has none. Filtered through classify() the
    same way, and skips anything already confirmed/corrected by later work
    (OCR, subtitle, manual review) even though it's still sitting in the
    original flagged set.
    """
    import csv as _csv
    from . import config, join as _join

    flagged = db.query("""
        SELECT fa.track_id, fa.facility_id, fa.match_status, a.track_name,
               a.description, a.subtitle, v.vendor,
               a.seller_url, a.support_url, a.privacy_policy_url, a.developer_website,
               f.facility_name, f.city, f.state_code
        FROM ga_facility_app fa
        JOIN ga_app a ON a.track_id = fa.track_id
        JOIN facility f ON f.facility_id = fa.facility_id
        LEFT JOIN ga_app_vendor v ON v.track_id = a.track_id
        WHERE fa.match_method = 'match_service'
    """)

    items, index, meta = [], [], {}
    for r in flagged:
        status = (r["match_status"] or "").lower()
        if "confirmed" in status or "corrected" in status:
            continue
        name, city, state = _join.candidate_name(r)
        if not name or len(name) < 3:
            continue
        item = {"facility_name": name}
        if city:
            item["city"] = city
        code = _join.normalize_state(state)
        if code:
            item["state"] = code
        items.append(item)
        index.append(r["track_id"])
        meta[r["track_id"]] = r

    in_scope = {row["facility_id"] for row in db.query(
        f"SELECT facility_id FROM facility WHERE {config.FACILITY_WHERE}")}

    collisions = []
    for i in range(0, len(items), config.MATCH_BATCH_SIZE):
        chunk = items[i:i + config.MATCH_BATCH_SIZE]
        ids = index[i:i + config.MATCH_BATCH_SIZE]
        import requests as _requests
        r = _requests.post(f"{config.MATCH_API_BASE}/match/facility/bulk",
                           json={"confidence_threshold": config.MATCH_CONFIDENCE_THRESHOLD,
                                 "include_candidates": True, "requests": chunk}, timeout=180)
        r.raise_for_status()
        job_id = r.json()["job_id"]
        results = _join._poll(job_id, total=len(chunk))
        for tid, res in zip(ids, results):
            cands = res.get("candidates") or []
            top_conf = max((c.get("confidence", 0) for c in cands), default=0)
            competing = [c for c in cands if c.get("confidence", 0) >= top_conf - 0.001]
            in_scope_competing = [c for c in competing if int(c.get("facility_id", -1)) in in_scope]
            if len(in_scope_competing) > 1:
                collisions.append((tid, in_scope_competing))

    def urls(r):
        vals = [r.get(k) for k in ("seller_url", "support_url", "privacy_policy_url",
                                   "developer_website") if r.get(k)]
        return " | ".join(vals)

    out = []
    for tid, cands in collisions:
        r = meta[tid]
        if classify(r, r["vendor"]):
            continue
        cand_labels = "; ".join(f"#{c.get('facility_id')} {c.get('facility_name', '')}"
                                for c in cands)
        out.append({
            "track_id": tid, "app_name": r["track_name"], "vendor": r["vendor"] or "",
            "app_store_url": f"https://apps.apple.com/us/app/id{tid}",
            "current_link": f"#{r['facility_id']} {r['facility_name']} | {r['city']}, {r['state_code']}",
            "competing_candidates": cand_labels, "app_urls": urls(r),
            "CORRECT_FACILITY_ID": "", "NOTES": "",
        })

    header = ["track_id", "app_name", "vendor", "app_store_url", "current_link",
             "competing_candidates", "app_urls", "CORRECT_FACILITY_ID", "NOTES"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        w.writerows(out)

    return {"path": path, "checked": len(items), "collisions": len(out)}


def classify(app: dict, vendor: str | None) -> str | None:
    """Single source of truth for 'is this app even a candidate for Lens/
    needs-linking review, or should it be pulled out entirely.' Returns an
    exclude reason string, or None if it's a legitimate candidate. Every
    needs-lens/needs-linking export should filter through this rather than
    reimplementing the checks inline.

    lacks_golf_signal() dropped from here 2026-08-21 (Derek): tested it
    directly by running all 489 apps it was excluding through the real
    waterfall (domain -> OCR -> subtitle -> match-service) with the filter
    bypassed -- 480/489 (98.2%) resolved cleanly to a real, in-scope
    facility, mostly via domain match (251, near-exact-identity) and
    match-service (217). The filter was excluding almost everything it
    touched, and the underlying matching techniques already provide the
    false-positive protection it was meant for -- a domain or match-service
    hit is real corroborating evidence, not a name-collision guess the way a
    bare keyword check is. See docs/no_golf_signal_resolved.csv."""
    if not vendor:
        return "no_vendor_signature"
    non_us = likely_non_us(app)
    if non_us:
        return f"non_us: {non_us}"
    return None


def run_with_matcher(track_ids: list[int], apply_links: bool = True) -> PipelineStats:
    """Stage A: Lens + app-name query -> city/state. Stage B: match-service-2-0.
    A link is written only if Stage B returns a real, in-scope match."""
    from . import config, join

    stats = PipelineStats()
    key = api_key()
    apps = {r["track_id"]: r for r in db.query(
        f"SELECT track_id, track_name, description, artwork_url, subtitle, "
        f"seller_url, support_url, privacy_policy_url, developer_website "
        f"FROM ga_app WHERE track_id IN ({','.join(str(t) for t in track_ids)})"
    )}
    stats.considered = len(track_ids)
    non_us_skipped = 0
    no_golf_signal_skipped = 0
    in_scope = {r["facility_id"] for r in db.query(
        f"SELECT facility_id FROM facility WHERE {config.FACILITY_WHERE}")}

    # Resume support: a prior attempt may have checkpointed Stage A (spent the
    # credit, saved query/city/state) before Stage B ran or crashed. Reuse that
    # instead of paying for the same Lens call twice.
    prior = {r["track_id"]: r for r in db.query(
        f"SELECT track_id, query_used, extracted_city, extracted_state, hosts, raw "
        f"FROM ga_lens_detection WHERE track_id IN ({','.join(str(t) for t in track_ids)}) "
        f"AND query_used IS NOT NULL AND matcher_status IS NULL"
    )}
    if prior:
        log.info("resuming %d apps from a prior Stage-A checkpoint (no new Lens "
                 "calls for these)", len(prior))

    lens_out: dict[int, dict] = {}
    for tid, r in prior.items():
        lens_out[tid] = {
            "query": r["query_used"], "city": r["extracted_city"],
            "state": r["extracted_state"],
            "hosts": [(h["host"], h["score"]) for h in (r["hosts"] or [])],
            "raw": r["raw"] or {},
        }

    missing_artwork_backfilled = 0
    for i, tid in enumerate(track_ids, 1):
        if tid in prior:
            continue
        a = apps.get(tid)
        if not a:
            log.warning("skipping %s -- not in ga_app", tid)
            continue
        non_us = likely_non_us(a)
        if non_us:
            non_us_skipped += 1
            log.info("skipping %s -- looks non-US (%s), not worth a Lens credit "
                     "or the false-match risk", tid, non_us)
            continue
        if lacks_golf_signal(a):
            no_golf_signal_skipped += 1
            log.info("skipping %s -- no 'golf' or 'tee time' in name/description, "
                     "not worth the false-match risk on a shared club name", tid)
            continue
        if not a.get("artwork_url"):
            # Not a data gap worth backfilling wholesale -- most of the corpus
            # never needed an icon before now. But an app that's actually
            # reaching Lens (failed every other resolution path) DOES need
            # one, and Apple always has it; a plain iTunes lookup is free
            # (unlike the SerpAPI call about to follow), so fetch it here
            # instead of silently dropping the app from the sample.
            try:
                r = requests.get("https://itunes.apple.com/lookup",
                                 params={"id": tid, "country": "us"}, timeout=20)
                results = r.json().get("results", [])
                art = (results[0].get("artworkUrl512") or results[0].get("artworkUrl100")
                      if results else None)
            except Exception as e:
                log.warning("artwork backfill lookup failed for %s: %s", tid, e)
                art = None
            if not art:
                log.warning("skipping %s -- no artwork_url even after live lookup", tid)
                continue
            a["artwork_url"] = art
            db.exec_sql(f"UPDATE ga_app SET artwork_url = "
                       f"'{art.replace(chr(39), chr(39)+chr(39))}' WHERE track_id = {tid}")
            missing_artwork_backfilled += 1
        cleaned = join.clean_name(a.get("track_name")) or a.get("track_name")
        try:
            resp = lens_search(a["artwork_url"], key, query=cleaned)
            stats.called += 1
        except Exception as e:
            stats.api_errors += 1
            log.warning("lens failed for %s: %s", tid, e)
            if stats.api_errors >= 5 and stats.called == 0:
                raise RuntimeError(
                    f"5 consecutive failures, 0 successes -- stopping. Last: {e}")
            continue

        # Stage A's own extraction is now inside the per-item try/except too. A
        # malformed response shape here previously crashed the whole batch AFTER
        # already paying for the API call, with nothing persisted to show for it --
        # exactly what happened on the 50-item run (19 credits spent, 0 rows saved,
        # because the DB write only happened after Stage B, and the process was
        # killed by a double-background/orphan bug before reaching it).
        try:
            city, state = extract_city_state(resp)
            hosts = candidate_hosts(resp)
        except Exception as e:
            stats.api_errors += 1
            log.warning("extraction failed for %s: %s", tid, e)
            continue
        if city:
            stats.with_city_state += 1
        lens_out[tid] = {"query": cleaned, "city": city, "state": state,
                         "hosts": hosts, "raw": resp}

        # Checkpoint Stage A immediately -- paid-for results must survive a crash in
        # Stage B (a match-service hiccup) or in the process itself.
        if i % 10 == 0 or i == len(track_ids):
            db.upsert("ga_lens_detection", [
                {"track_id": t, "query_used": lo["query"],
                 "extracted_city": lo["city"], "extracted_state": lo["state"],
                 "hosts": [{"host": h, "score": s} for h, s in lo["hosts"][:10]],
                 "raw": {"visual_matches": (lo["raw"].get("visual_matches") or [])[:15]}}
                for t, lo in lens_out.items()
            ], on_conflict="track_id")
            log.info("  stage A checkpoint: %d/%d done, %d credits used so far",
                     i, len(track_ids), stats.called)

    if not lens_out:
        return stats

    # ---- Stage B: submit through the SAME matcher Phase 2 trusts ----------------
    items, ids = [], []
    for tid, lo in lens_out.items():
        item = {"facility_name": lo["query"]}
        if lo["city"]:
            item["city"] = lo["city"]
        code = join.normalize_state(lo["state"])
        if code:
            item["state"] = code
        items.append(item)
        ids.append(tid)

    stats.submitted_to_matcher = len(items)
    try:
        job = join._submit(items, config.MATCH_CONFIDENCE_THRESHOLD)
        results = join._poll(job, total=len(items))
    except Exception as e:
        # Stage A is already checkpointed -- a Stage B failure must not look like
        # nothing happened. Report it plainly rather than raising past it silently.
        log.error("Stage B (match-service) failed after Stage A succeeded for %d "
                 "apps: %s. Stage A results ARE saved; re-run to retry Stage B only.",
                 len(items), e)
        raise
    if len(results) != len(items):
        raise RuntimeError(
            f"matcher returned {len(results)} results for {len(items)} requests; "
            f"positional mapping would misattribute every app")

    links, records = [], []
    for tid, res in zip(ids, results):
        lo = lens_out[tid]
        status = res.get("matchStatus") or "unknown"
        fid = res.get("facility_id")
        rejected = False
        if fid and int(fid) not in in_scope:
            rejected, fid = True, None
            stats.rejected_out_of_scope += 1

        if fid:
            stats.matched += 1
            links.append({"facility_id": int(fid), "track_id": tid,
                          "match_confidence": res.get("confidence"),
                          "match_method": "lens_matcher",
                          "match_status": f"Lens+matcher - {status}"})
        else:
            stats.no_match += 1

        records.append({
            "track_id": tid, "query_used": lo["query"],
            "extracted_city": lo["city"], "extracted_state": lo["state"],
            "hosts": [{"host": h, "score": s} for h, s in lo["hosts"][:10]],
            "matcher_facility_id": int(fid) if fid else None,
            "matcher_status": status + (" (rejected: out of scope)" if rejected else ""),
            "matcher_confidence": res.get("confidence"),
            "raw": {"visual_matches": (lo["raw"].get("visual_matches") or [])[:15]},
        })
        stats.samples.append({
            "track_id": tid, "app_name": apps[tid]["track_name"],
            "query": lo["query"], "city": lo["city"], "state": lo["state"],
            "matcher_status": status, "matcher_facility_id": fid,
        })

    db.upsert("ga_lens_detection", records, on_conflict="track_id")
    stats.new_links = len(links)
    if apply_links and links:
        db.upsert("ga_facility_app", links, on_conflict="facility_id,track_id")

    try:
        acct = requests.get("https://serpapi.com/account",
                            params={"api_key": key}, timeout=30).json()
        stats.credits_remaining = acct.get("plan_searches_left")
    except Exception:
        pass

    stats.artwork_backfilled = missing_artwork_backfilled
    stats.non_us_skipped = non_us_skipped
    stats.no_golf_signal_skipped = no_golf_signal_skipped
    log.info("lens+matcher done: %s", stats.as_dict())
    return stats


MATCHER_CALIBRATION_HEADER = [
    "track_id", "app_name", "app_store_url",
    "query_used", "extracted_city_state",
    "matcher_status", "proposed_facility", "currently_linked",
    "CORRECT [y/n]", "ACTUAL_FACILITY_ID", "NOTES",
]


def export_matcher_calibration(path: str, track_ids: list[int] | None = None) -> dict:
    """Every app that went through the two-stage (Lens+query -> match-service)
    pipeline, for grading. Deliberately includes the 'No Match' and rejected rows,
    not just the ones that linked -- correctly declining is exactly as important to
    verify as correctly linking, and this is how we would catch, e.g., an app that
    SHOULD have matched but the matcher wrongly said No Match.
    """
    import csv as _csv
    where = f"WHERE l.track_id IN ({','.join(str(t) for t in track_ids)})" if track_ids else ""
    rows = db.query(f"""
        SELECT l.track_id, a.track_name, l.query_used, l.extracted_city,
               l.extracted_state, l.matcher_status, l.matcher_facility_id,
               f.facility_name, f.city, f.state_code,
               EXISTS (SELECT 1 FROM ga_facility_app fa
                      WHERE fa.track_id = l.track_id
                        AND fa.match_method = 'lens_matcher') AS currently_linked
        FROM ga_lens_detection l
        JOIN ga_app a ON a.track_id = l.track_id
        LEFT JOIN facility f ON f.facility_id = l.matcher_facility_id
        {where}
        ORDER BY currently_linked DESC, l.matcher_status NULLS LAST, l.track_id
    """)
    out = []
    for r in rows:
        cs = (f"{r['extracted_city'] or ''}, {r['extracted_state'] or ''}"
             if (r["extracted_city"] or r["extracted_state"]) else "")
        prop = (f"{r['matcher_facility_id']} | {r['facility_name']} | "
               f"{r['city']}, {r['state_code']}") if r["matcher_facility_id"] else ""
        out.append({
            "track_id": r["track_id"], "app_name": r["track_name"],
            "app_store_url": f"https://apps.apple.com/us/app/id{r['track_id']}",
            "query_used": r["query_used"] or "", "extracted_city_state": cs,
            "matcher_status": r["matcher_status"] or "(no Lens result)",
            "proposed_facility": prop,
            "currently_linked": "yes" if r["currently_linked"] else "",
            "CORRECT [y/n]": "", "ACTUAL_FACILITY_ID": "", "NOTES": "",
        })
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=MATCHER_CALIBRATION_HEADER)
        w.writeheader()
        w.writerows(out)
    return {"path": path, "rows": len(out),
            "linked": sum(1 for r in out if r["currently_linked"])}


def run(track_ids: list[int], apply_links: bool = True) -> LensStats:
    """Process an EXPLICIT list of track_ids. No unbounded default -- the free tier
    is 250 searches/month total, and every call here is one search."""
    stats = LensStats()
    key = api_key()
    facs = domainmatch.facility_hosts()

    done = {r["track_id"] for r in db.query("SELECT track_id FROM ga_lens_detection")}
    todo = [t for t in track_ids if t not in done]
    stats.cached = len(track_ids) - len(todo)
    stats.considered = len(todo)
    log.info("lens: %d to call (%d already cached)", len(todo), stats.cached)

    apps = {r["track_id"]: r for r in db.query(
        f"SELECT track_id, track_name, artwork_url FROM ga_app "
        f"WHERE track_id IN ({','.join(str(t) for t in todo)})"
    )} if todo else {}

    links, records = [], []
    for i, tid in enumerate(todo, 1):
        a = apps.get(tid)
        if not a or not a.get("artwork_url"):
            log.warning("skipping %s -- no artwork_url", tid)
            continue
        try:
            resp = lens_search(a["artwork_url"], key)
            stats.called += 1
        except Exception as e:
            stats.api_errors += 1
            log.warning("lens failed for %s: %s", tid, e)
            if stats.api_errors >= 5 and stats.called == 0:
                raise RuntimeError(
                    f"5 consecutive failures, 0 successes -- stopping. Last: {e}")
            continue

        hosts = candidate_hosts(resp)
        city, state = extract_city_state(resp)
        if hosts:
            stats.with_candidate_hosts += 1
        if city:
            stats.with_city_state += 1

        resolved_id = resolved_host = None
        for h, _score in hosts:
            m = facs.get(h) or facs.get(domainmatch.registrable(h))
            if m and len({x["facility_id"] for x in m}) == 1:
                resolved_id, resolved_host = m[0]["facility_id"], h
                break

        records.append({
            "track_id": tid,
            "hosts": [{"host": h, "score": s} for h, s in hosts[:10]],
            "extracted_city": city, "extracted_state": state,
            "resolved_facility_id": resolved_id, "resolved_host": resolved_host,
            "raw": {"visual_matches": (resp.get("visual_matches") or [])[:15],
                   "organic_results": (resp.get("organic_results") or [])[:10]},
        })
        stats.samples.append({
            "track_id": tid, "app_name": a.get("track_name"),
            "top_hosts": [h for h, _ in hosts[:3]], "city": city, "state": state,
            "resolved": bool(resolved_id),
        })

        if resolved_id:
            stats.resolved_to_facility += 1
            links.append({
                "facility_id": resolved_id, "track_id": tid,
                "match_confidence": 0.9, "match_method": "lens",
                "match_status": f"Match - Lens Web Detection ({resolved_host})",
            })

        if i % 10 == 0:
            log.info("  %d/%d resolved=%d city_state=%d", i, len(todo),
                     stats.resolved_to_facility, stats.with_city_state)
            db.upsert("ga_lens_detection", records, on_conflict="track_id")
            records = []

    if records:
        db.upsert("ga_lens_detection", records, on_conflict="track_id")
    stats.new_links = len(links)
    if apply_links and links:
        db.upsert("ga_facility_app", links, on_conflict="facility_id,track_id")

    try:
        acct = requests.get("https://serpapi.com/account",
                            params={"api_key": key}, timeout=30).json()
        stats.credits_remaining = acct.get("plan_searches_left")
    except Exception:
        pass

    log.info("lens done: %s", stats.as_dict())
    return stats
