"""Channel 3 -- identify a club from its app icon via Cloud Vision WEB_DETECTION.

The idea (Derek's, 2026-08-18) and why it is the right shape: don't ask an image
"which facility is this?", ask it "where else does this logo appear on the web?".
WEB_DETECTION returns pagesWithMatchingImages, and a club's logo overwhelmingly
appears on the club's own site. That yields a DOMAIN -- and domain -> facility is
already solved and measured in domainmatch.py (694 links). So this channel plugs
into an existing, validated resolver rather than inventing a fuzzy new one.

It also beats picking from the match service's 3-candidate shortlist, because the
correct facility is not necessarily IN that shortlist.

Two implementation details that matter:

  * CROP FIRST. White-label vendors composite the club logo onto a house template --
    every Gallus icon is the same golf ball on grass. WEB_DETECTION on the full icon
    tends to match the TEMPLATE, returning other Gallus apps rather than the club.
    Cropping to the logo region is what makes this work rather than merely look like
    it works.
  * CACHE HARD. Icons never change and calls cost money, so every response is stored
    raw in ga_web_detection and never re-fetched. Parsing can be revised offline.

Cost: $3.50/1,000 images, first 1,000 free per month, so the current 581-app queue
is free and the whole corpus is single-digit dollars.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
from dataclasses import dataclass, field

import requests

from . import db, domainmatch

log = logging.getLogger(__name__)

VISION_ENDPOINT = "https://vision.googleapis.com/v1/images:annotate"

# Hosts that can never identify a club. App Store mirrors and app-aggregator sites
# WILL come back for these icons -- they are where the icon legitimately appears.
NEVER_A_CLUB = {
    "apple.com", "itunes.apple.com", "apps.apple.com", "mzstatic.com",
    "play.google.com", "google.com", "gstatic.com", "facebook.com", "fb.com",
    "instagram.com", "twitter.com", "x.com", "linkedin.com", "youtube.com",
    "pinterest.com", "reddit.com", "wikipedia.org", "amazon.com",
    "appadvice.com", "apkpure.com", "apkcombo.com", "appbrain.com", "app-liste.com",
    "appstore.com", "apptopia.com", "sensortower.com", "similarweb.com",
    "producthunt.com", "softonic.com", "apkmonk.com", "appsonwindows.com",
    "qwant.com", "bing.com", "yandex.com", "alamy.com", "shutterstock.com",
    "istockphoto.com", "gettyimages.com", "dreamstime.com", "vecteezy.com",
    "logolynx.com", "seeklogo.com", "brandsoftheworld.com", "worldvectorlogo.com",
}

# Fraction of the icon to keep when cropping the vendor template off. The logo sits
# centred; the template contributes the outer band.
CROP_BOX = (0.16, 0.26, 0.84, 0.74)


@dataclass
class WebDetectStats:
    considered: int = 0
    cached: int = 0
    called: int = 0
    api_errors: int = 0
    with_candidate_hosts: int = 0
    resolved_to_facility: int = 0
    new_links: int = 0
    unresolved: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["unresolved"] = self.unresolved[:30]
        return d


def api_key() -> str:
    k = os.getenv("GOOGLE_VISION_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not k:
        raise RuntimeError(
            "No key. Set GOOGLE_VISION_API_KEY (or GEMINI_API_KEY) and make sure "
            "Cloud Vision API is ENABLED for the project and the key is not "
            "restricted to other APIs -- a restricted key returns 403 "
            "API_KEY_SERVICE_BLOCKED.")
    return k


def crop_logo(png: bytes) -> bytes:
    """Strip the vendor template band so the club logo dominates the image."""
    try:
        from PIL import Image
    except ImportError:
        log.warning("Pillow not installed -- sending the uncropped icon, which is "
                    "likely to match the vendor template instead of the club")
        return png
    im = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = im.size
    box = (int(w * CROP_BOX[0]), int(h * CROP_BOX[1]),
           int(w * CROP_BOX[2]), int(h * CROP_BOX[3]))
    out = io.BytesIO()
    im.crop(box).save(out, format="PNG")
    return out.getvalue()


def detect(png: bytes, key: str, max_results: int = 20) -> dict:
    body = {"requests": [{
        "image": {"content": base64.b64encode(png).decode()},
        "features": [{"type": "WEB_DETECTION", "maxResults": max_results}],
    }]}
    r = requests.post(f"{VISION_ENDPOINT}?key={key}", json=body, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"Vision {r.status_code}: {r.text[:300]}")
    resp = r.json()["responses"][0]
    if "error" in resp:
        raise RuntimeError(f"Vision error: {resp['error']}")
    return resp.get("webDetection", {}) or {}


def candidate_hosts(web: dict) -> list[tuple[str, int]]:
    """Rank plausible club domains out of a WEB_DETECTION response."""
    skip = domainmatch.vendor_hosts() | NEVER_A_CLUB
    counts: dict[str, int] = {}
    # pagesWithMatchingImages is the highest-signal field: the actual pages the logo
    # appears on. Full/partial matching image URLs are weaker but still useful.
    for field_name, weight in (("pagesWithMatchingImages", 3),
                               ("fullMatchingImages", 2),
                               ("partialMatchingImages", 1)):
        for item in web.get(field_name, []) or []:
            h = domainmatch.host(item.get("url"))
            if not h:
                continue
            if any(h == s or h.endswith("." + s) for s in skip):
                continue
            counts[h] = counts.get(h, 0) + weight
    return sorted(counts.items(), key=lambda kv: -kv[1])


def run(limit: int | None = None, apply_links: bool = True,
        only_unmatched: bool = True) -> WebDetectStats:
    stats = WebDetectStats()
    key = api_key()
    facs = domainmatch.facility_hosts()

    where = ("AND NOT EXISTS (SELECT 1 FROM ga_facility_app f "
             "WHERE f.track_id = a.track_id)") if only_unmatched else ""
    # NOT restricted to vendor IS NOT NULL. An unlabelled app is the case that most
    # needs identifying -- Twin Hills CC was invisible to this pass purely because its
    # vendor (Versant) was missing from vendors.yaml, which has nothing to do with
    # whether its logo is identifiable.
    rows = db.query(f"""
        SELECT a.track_id, a.track_name, a.artwork_url, v.vendor
        FROM ga_app a
        LEFT JOIN ga_app_vendor v ON v.track_id = a.track_id
        WHERE a.delisted_at IS NULL
          AND a.artwork_url IS NOT NULL
          AND (v.vendor IS NOT NULL
               OR a.description ILIKE '%tee time%'
               OR a.description ILIKE '%golf club%'
               OR a.description ILIKE '%golf course%')
          {where}
        ORDER BY a.track_id
    """)
    done = {r["track_id"] for r in db.query("SELECT track_id FROM ga_web_detection")}
    rows = [r for r in rows if r["track_id"] not in done]
    stats.cached = len(done)
    if limit:
        rows = rows[:limit]
    stats.considered = len(rows)
    log.info("web detection: %d apps to process (%d already cached)",
             len(rows), len(done))

    sess = requests.Session()
    links, records = [], []
    for i, r in enumerate(rows, 1):
        try:
            png = sess.get(r["artwork_url"], timeout=60).content
            web = detect(crop_logo(png), key)
            stats.called += 1
        except Exception as e:
            stats.api_errors += 1
            log.warning("web detection failed for %s: %s", r["track_id"], e)
            if stats.api_errors >= 5 and stats.called == 0:
                raise RuntimeError(
                    "5 consecutive failures with zero successes -- stopping rather "
                    f"than burning quota. Last error: {e}")
            continue

        hosts = candidate_hosts(web)
        entities = [e.get("description") for e in (web.get("webEntities") or [])
                    if e.get("description")][:10]
        if hosts:
            stats.with_candidate_hosts += 1

        resolved_id = resolved_host = None
        for h, _score in hosts:
            match = facs.get(h)
            if match and len({m["facility_id"] for m in match}) == 1:
                resolved_id = match[0]["facility_id"]
                resolved_host = h
                break

        records.append({
            "track_id": r["track_id"], "image_variant": "cropped",
            "hosts": [{"host": h, "score": s} for h, s in hosts[:10]],
            "web_entities": entities,
            "resolved_facility_id": resolved_id, "resolved_host": resolved_host,
            "raw": {k: web.get(k) for k in ("webEntities", "pagesWithMatchingImages")},
        })

        if resolved_id:
            stats.resolved_to_facility += 1
            links.append({
                "facility_id": resolved_id, "track_id": r["track_id"],
                "match_confidence": 0.9, "match_method": "web_detection",
                "match_status": f"Match - Logo Web Detection ({resolved_host})",
            })
        else:
            stats.unresolved.append({
                "track_id": r["track_id"], "track_name": r["track_name"],
                "vendor": r["vendor"], "top_hosts": [h for h, _ in hosts[:5]],
                "entities": entities[:5],
            })

        if i % 25 == 0:
            log.info("  %d/%d  resolved=%d", i, len(rows), stats.resolved_to_facility)
            db.upsert("ga_web_detection", records, on_conflict="track_id")
            records = []

    if records:
        db.upsert("ga_web_detection", records, on_conflict="track_id")
    stats.new_links = len(links)
    if apply_links and links:
        db.upsert("ga_facility_app", links, on_conflict="facility_id,track_id")

    log.info("web detection done: %s", stats.as_dict())
    return stats
