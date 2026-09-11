"""apps.apple.com HTML fetch and field extraction.

Needed because `copyright`, `privacyPolicyUrl` and the support URL exist ONLY on the
HTML page -- they are absent from the iTunes JSON (Check 1). Those are the fields
that resolve vendor switches, so this pass is how the hard cases get settled.

Two measured facts shape this module:
  * The page is Fastly-cached and served in ~0.19s; 60 sequential fetches produced
    zero blocks (316 req/min). This is NOT the bottleneck the plan budgeted 2h for.
  * The `shoebox-media-api-cache-apps` blob that older Apple scrapers target is
    gone. Today's page has `serialized-server-data` plus server-rendered DOM, so
    extraction reads the rendered <dt>/<dd> pairs and labelled <a> tags.
"""
from __future__ import annotations

import html as htmllib
import logging
import re
import time

import requests

from . import config

log = logging.getLogger(__name__)

_COPYRIGHT_RE = re.compile(r"<dt[^>]*>Copyright</dt>\s*<dd><ul><li[^>]*>(.*?)</li>", re.S)
_SELLER_RE = re.compile(r"<dt[^>]*>Seller</dt>\s*<dd><ul><li[^>]*>(.*?)</li>", re.S)
_LINK_RE = re.compile(r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', re.S)
_PRIVACY_ARIA_RE = re.compile(
    r'aria-label="Developer.{0,3}s Privacy Policy"[^>]*href="([^"]+)"')
_SUPPORT_RE = re.compile(r'"url":"(https?://[^"]*support[^"]*)"')
_APP_ID_RE = re.compile(r"/app/[^\"']*?/id(\d{6,12})")
# App Store Connect's optional "subtitle" field, rendered as <p class="subtitle">
# right under the app's <h1> title on the product page -- absent from the free
# iTunes JSON entirely. Several vendors (Chronogolf confirmed, maybe others) set
# it to "City, State", which is a clean, image-free location signal (Derek,
# 2026-08-19). BUG FOUND 2026-08-19: the page also embeds a "similar apps"
# carousel with OTHER apps' subtitles in raw "subtitle":"..." JSON, appearing
# BEFORE this app's own -- matching the first occurrence in the page text grabs
# a random unrelated app's tagline (produced "Book Tee-Times & Deals" for
# every single app tested, since that carousel entry recurs across pages).
# The rendered <p class="subtitle"> tag is unambiguous -- exactly one, and it's
# what a human actually sees on the page -- so match that instead.
_SUBTITLE_RE = re.compile(r'<p class="subtitle[^"]*"[^>]*>(.*?)</p>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(s: str | None) -> str | None:
    if not s:
        return None
    s = re.sub(r"<!--.*?-->", "", s)
    s = htmllib.unescape(_TAG_RE.sub(" ", s))
    return re.sub(r"\s+", " ", s).strip() or None


class AppStoreClient:
    def __init__(self, min_interval: float = config.HTML_MIN_INTERVAL_S,
                 proxies: dict | None = None, pool_size: int = 10):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": config.USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        })
        if proxies:
            # A degraded/penalized direct IP still blocks apps.apple.com even
            # though it's a normally-open endpoint (316 rpm, 0 blocks measured
            # -- see findings.md Check 5d) -- found 2026-08-23 when the crawl
            # step failed right after a heavy night of direct-IP search-API
            # traffic on a DIFFERENT Apple endpoint. Routing through a proxy
            # sidesteps whatever shared reputation signal caused that.
            from requests.adapters import HTTPAdapter
            adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size,
                                  max_retries=0)
            self.s.mount("http://", adapter)
            self.s.mount("https://", adapter)
        self.proxies = proxies
        self.min_interval = min_interval
        self._last = 0.0
        self.fetched = 0
        self.errors = 0

    def fetch(self, track_id: int) -> str | None:
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        url = config.APPSTORE_PAGE.format(track_id=track_id)
        try:
            r = self.s.get(url, timeout=45, proxies=self.proxies)
        except requests.RequestException as e:
            log.warning("html fetch failed for %s: %s", track_id, e)
            self.errors += 1
            self._last = time.time()
            return None
        self._last = time.time()
        self.fetched += 1
        if r.status_code == 404:
            return "__DELISTED__"
        if r.status_code != 200:
            log.warning("html %s for %s", r.status_code, track_id)
            self.errors += 1
            return None
        # Apple omits the charset on some responses, so requests falls back to
        # latin-1 and copyright lines come out as 'Â© 2022 Gallus Golf'. The page is
        # always UTF-8; say so explicitly rather than letting it be guessed.
        r.encoding = "utf-8"
        return r.text


def extract(page: str) -> dict:
    """Pull the vendor-bearing fields out of a product page."""
    out: dict = {
        "copyright": _clean(m.group(1)) if (m := _COPYRIGHT_RE.search(page)) else None,
        "seller_name_html": _clean(m.group(1)) if (m := _SELLER_RE.search(page)) else None,
        "subtitle": _clean(m.group(1)) if (m := _SUBTITLE_RE.search(page)) else None,
    }

    links: dict[str, str] = {}
    for m in _LINK_RE.finditer(page):
        label = _clean(m.group(2))
        if label and len(label) < 60:
            links.setdefault(label, m.group(1))
    out["developer_website"] = links.get("Developer Website")
    out["app_support"] = links.get("App Support")

    privacy = links.get("Privacy Policy")
    if not privacy and (m := _PRIVACY_ARIA_RE.search(page)):
        privacy = m.group(1)
    out["privacy_policy_url"] = privacy

    support = out.get("app_support")
    if not support and (m := _SUPPORT_RE.search(page)):
        support = m.group(1)
    out["support_url"] = support

    # The recommendation graph is already embedded in this page -- the separate
    # ?see-all=customers-also-bought-apps request the plan specifies is redundant.
    out["related_track_ids"] = sorted({int(i) for i in _APP_ID_RE.findall(page)})
    return out


def enrich_row(track_id: int, page: str) -> dict:
    """Merge extracted HTML fields into a ga_app update row."""
    f = extract(page)
    return {
        "track_id": track_id,
        "copyright": f["copyright"],
        "privacy_policy_url": f["privacy_policy_url"],
        "support_url": f["support_url"],
        "developer_website": f["developer_website"],
        "subtitle": f["subtitle"],
        "html_fetched_at": _utcnow(),
    }


def _utcnow() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
