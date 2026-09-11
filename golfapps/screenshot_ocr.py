"""Channel 4 -- identify a club from the address printed in its first App Store
screenshot. ClubCaddie's template banner puts the club name + street address
(sometimes city/state/zip too) right at the top of screenshot #1 (Derek,
2026-08-19). OCR that text, then match against facility.facility_street_address
and facility.zip -- ZIP alone narrows the candidate set to a handful of
facilities nationwide, so this is a much higher-precision signal than name
matching once a street number confirms it.
"""
from __future__ import annotations

import base64
import logging
import re

import requests

from . import config, db

log = logging.getLogger(__name__)

VISION_ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"
_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
# "1021 Brockton Mountain Dr", "608 W Lakeview Drive", "N10500 Indian Point Road"
# (Wisconsin/Michigan township addressing prefixes the house number with a
# direction letter) -- a leading house number is the strongest single token an
# OCR pass can hand back.
_STREET_RE = re.compile(
    r"\b([NSEW]?\d{1,6}\s+[A-Za-z0-9.'\- ]{3,40}?\s+"
    r"(?:St|Street|Ave|Avenue|Dr|Drive|Rd|Road|Blvd|Boulevard|Ln|Lane|Way|Pkwy|"
    r"Parkway|Ct|Court|Cir|Circle|Pl|Place|Hwy|Highway|Trl|Trail)\b\.?)",
    re.I)


def api_key() -> str:
    import os
    k = os.getenv("GOOGLE_VISION_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not k:
        raise RuntimeError("No GOOGLE_VISION_API_KEY in the environment.")
    return k


def first_screenshot_url(track_id: int) -> str | None:
    r = requests.get("https://itunes.apple.com/lookup",
                     params={"id": track_id, "country": "us"}, timeout=30)
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        return None
    shots = results[0].get("screenshotUrls") or []
    return shots[0] if shots else None


def ocr_text(png: bytes, key: str) -> str:
    body = {"requests": [{
        "image": {"content": base64.b64encode(png).decode()},
        "features": [{"type": "TEXT_DETECTION", "maxResults": 5}],
    }]}
    r = requests.post(f"{VISION_ENDPOINT}?key={key}", json=body, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Vision {r.status_code}: {r.text[:300]}")
    resp = r.json()["responses"][0]
    if "error" in resp:
        raise RuntimeError(f"Vision error: {resp['error']}")
    ann = resp.get("textAnnotations") or []
    return ann[0]["description"] if ann else ""


def extract_address(text: str) -> dict:
    streets = [s.strip() for s in _STREET_RE.findall(text)]
    # A 5-digit street HOUSE NUMBER ("75800 Avondale Drive") matches _ZIP_RE too
    # -- drop any "zip" that is actually just a street's leading number, or a
    # real zip gets replaced by a garbage one and the facility query misses.
    house_numbers = {re.match(r"[NSEW]?(\d+)", s, re.I).group(1) for s in streets
                     if re.match(r"[NSEW]?(\d+)", s, re.I)}
    zips = [z for z in _ZIP_RE.findall(text) if z not in house_numbers and z != "00000"]
    return {"zip_candidates": zips, "street_candidates": streets}


_ABBREV = {"street": "st", "avenue": "ave", "drive": "dr", "road": "rd",
          "boulevard": "blvd", "lane": "ln", "parkway": "pkwy", "court": "ct",
          "circle": "cir", "place": "pl", "highway": "hwy", "trail": "trl",
          "north": "n", "south": "s", "east": "e", "west": "w"}


def _norm_street(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[.,]", "", s)
    s = re.sub(r"(?<=\d)-(?=\d)", "", s)   # "75-800" -> "75800"
    s = re.sub(r"\b(" + "|".join(_ABBREV) + r")\b", lambda m: _ABBREV[m.group(1)], s)
    return re.sub(r"\s+", " ", s).strip()


def _street_house_number(s: str) -> str | None:
    m = re.match(r"[nsew]?(\d+)", _norm_street(s))
    return m.group(1) if m else None


_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
_STATE_CODES = set(_STATE_NAMES.values())
# A bare 2-letter code is only trusted right before a zip ("Springdale, 72764,
# Arkansas" also happens, so the full-name search below is the primary path;
# this is a fallback for "City, ST 12345" layouts).
_CODE_NEAR_ZIP_RE = re.compile(r"\b([A-Z]{2})\s*\d{5}(?:-\d{4})?\b")

_NON_US_RE = re.compile(
    r"(?i)\b(canada|ontario|british columbia|alberta|quebec|nova scotia|"
    r"saskatchewan|manitoba|united kingdom|england|scotland|wales|ireland|"
    r"australia|new zealand|mexico|costa rica)\b")
_US_RE = re.compile(r"(?i)\bunited states\b|\bU\.?S\.?A\.?\b")


def extract_state_country(text: str) -> dict:
    """The simpler, more robust signal: just get state + country out of the
    screenshot text, then let match-service resolve (name, state) the same
    way it already resolves every other app -- no bespoke street/zip index
    needed. Full state names ("Arkansas") are the primary source since
    OCR reliably keeps whole words intact; a bare 2-letter code is only
    trusted immediately before a zip, to avoid matching stray letters."""
    low = text.lower()
    state = None
    for name, code in _STATE_NAMES.items():
        if re.search(r"\b" + re.escape(name) + r"\b", low):
            state = code
            break
    if not state:
        m = _CODE_NEAR_ZIP_RE.search(text)
        if m and m.group(1) in _STATE_CODES:
            state = m.group(1)

    non_us = _NON_US_RE.search(text)
    if non_us:
        country = non_us.group(1).title()
    elif state or _US_RE.search(text):
        country = "US"
    else:
        country = None
    return {"state": state, "country": country}


def match_facility(extracted: dict) -> list[dict]:
    """ZIP-first, confirmed by a normalized street-address prefix match. Falls
    back to a house-number search across the whole table when no zip was
    read -- common: several ClubCaddie templates show only the street line."""
    zips = extracted["zip_candidates"]
    streets = extracted["street_candidates"]

    if zips:
        cands = db.query(f"""
            SELECT facility_id, facility_name, city, state_code, zip, facility_street_address
            FROM facility WHERE zip IN ({','.join("'" + z + "'" for z in zips)})
        """)
    elif streets:
        nums = {n for s in streets if (n := _street_house_number(s))}
        if not nums:
            return []
        # A direction letter is sometimes glued straight onto the number with
        # no space ("W6603 County Road C") -- match both "6603 ..." and a
        # single leading letter + "6603...", or Crystal Lake Golf Course's own
        # correct address gets skipped for a coincidental same-number match
        # elsewhere (found 2026-08-19 auditing the residue OCR pass).
        clauses = " OR ".join(
            f"facility_street_address ILIKE '{n} %' OR "
            f"facility_street_address ILIKE '_{n} %'" for n in nums)
        cands = db.query(f"""
            SELECT facility_id, facility_name, city, state_code, zip, facility_street_address
            FROM facility WHERE facility_street_address IS NOT NULL AND ({clauses})
        """)
    else:
        return []

    if not streets:
        return cands
    street_norms = [_norm_street(s) for s in streets]
    out = []
    for c in cands:
        fa = _norm_street(c.get("facility_street_address") or "")
        if not fa:
            continue
        if any(fa == sn or fa.startswith(sn[:12]) or sn.startswith(fa[:12])
               for sn in street_norms):
            out.append(c)
    return out or cands


# Vendors confirmed to put the club's own street address/city/state in
# screenshot #1 -- not a shared demo template. Versant and Tee-On were tested
# and confirmed UNSAFE (2026-08-19): both reuse an identical generic
# screenshot across every client ("7580 Golf Channel Drive, Orlando, FL" /
# "Golf Club C.C., Conestogo, ON" respectively), so OCR-ing them would create
# a false-collision magnet, not real signal. Every other vendor is simply
# UNVERIFIED -- add here only after confirming screenshot #1 is genuinely
# club-specific for a sample of that vendor's apps, the same way ClubCaddie/
# Tenfore/Versant/Tee-On were each individually checked.
SAFE_VENDORS = {"clubcaddie", "tenfore"}


def run(track_ids: list[int]) -> dict:
    """Standard pre-Lens OCR pass. Callers pass whatever candidate track_ids
    are still unresolved; this filters to SAFE_VENDORS internally and OCRs
    only those. Returns {"resolved": [...], "considered": n} -- resolved
    entries carry (track_id, facility_id, confidence, status), found via
    state+name through match-service (simpler and more robust than the
    street/zip index -- Derek, 2026-08-19: "all you need is state and
    you'll be able to match usually with name and state"). Does not write
    anything; callers decide whether/how to apply results.
    """
    from . import join, config, lens as _lens
    import requests as _requests

    rows = db.query(f"""
        SELECT a.track_id, a.track_name, a.description, a.seller_url, a.support_url,
               a.privacy_policy_url, a.developer_website, v.vendor
        FROM ga_app a JOIN ga_app_vendor v ON v.track_id = a.track_id
        WHERE a.track_id IN ({','.join(str(t) for t in track_ids)})
          AND v.vendor IN ({','.join("'" + v + "'" for v in SAFE_VENDORS)})
    """)
    log.info("ocr: %d of %d candidates are in a vetted-safe vendor", len(rows), len(track_ids))
    # Only the non-US check applies here, not the full classify(). SAFE_VENDORS
    # (ClubCaddie, TenFore) are golf-only software -- unlike Chronogolf, which
    # also serves plain non-golf clubs -- so the vendor confirmation alone
    # already proves this is a golf app; running lacks_golf_signal() on top
    # wrongly dropped real apps whose App Store name/description never says
    # "golf" or "tee time" (e.g. "Spring Creek GC & 19th Hole", "WWCC",
    # "Highlands Country Club (CA)") (Derek, 2026-08-20).
    rows = [r for r in rows if not _lens.likely_non_us(r)]
    log.info("ocr: %d pass non-US pre-filter", len(rows))
    if not rows:
        return {"resolved": [], "considered": 0}

    key = api_key()
    extracted = {}
    for i, r in enumerate(rows, 1):
        tid = r["track_id"]
        try:
            url = first_screenshot_url(tid)
            if not url:
                continue
            png = _requests.get(url, timeout=30).content
            text = ocr_text(png, key)
            sc = extract_state_country(text)
            if sc["country"] and sc["country"] != "US":
                continue  # non-US -- correctly excluded, no match-service call
            if sc["state"]:
                extracted[tid] = sc["state"]
        except Exception as e:
            log.warning("ocr failed for %s: %s", tid, e)
        if i % 20 == 0:
            log.info("  ocr %d/%d", i, len(rows))

    by_id = {r["track_id"]: r for r in rows}
    items, index = [], []
    for tid, state in extracted.items():
        name = join.clean_name(by_id[tid]["track_name"])
        if not name or len(name) < 3:
            continue
        items.append({"facility_name": name, "state": state})
        index.append(tid)

    if not items:
        return {"resolved": [], "considered": len(rows)}

    in_scope = {r["facility_id"] for r in db.query(
        f"SELECT facility_id FROM facility WHERE {config.FACILITY_WHERE}")}
    job = join._submit(items, config.MATCH_CONFIDENCE_THRESHOLD)
    results = join._poll(job, total=len(items))

    resolved = []
    for tid, res in zip(index, results):
        fid = res.get("facility_id")
        if fid and int(fid) in in_scope:
            resolved.append({"track_id": tid, "facility_id": int(fid),
                             "match_confidence": res.get("confidence"),
                             "match_status": res.get("matchStatus"),
                             "match_method": "ocr_state_match"})
    log.info("ocr: %d/%d resolved", len(resolved), len(rows))
    return {"resolved": resolved, "considered": len(rows)}
