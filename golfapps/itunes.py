"""iTunes API client, paced from the measurements in docs/findings.md.

The three behaviours that matter, all of them measured rather than assumed:
  * `offset` does nothing and each query tops out near 190 results -> callers must
    split any query that comes back at the ceiling (see sweep.split_query).
  * 200 ids per lookup is clean; 300 returns 210 rows under an HTTP 200. Every
    batch asserts its own length because the failure is silent.
  * 429 is transient, 403 is a sticky penalty box. Backing off 403 in seconds
    lengthens the outage, so we stand down for ten minutes.
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from typing import Iterable, Iterator

import requests

from . import config

log = logging.getLogger(__name__)


class RateLimitPenalty(RuntimeError):
    """Raised when Apple returns 403 -- the caller should stop, not retry harder."""


class ITunesClient:
    def __init__(self, min_interval: float = config.API_MIN_INTERVAL_S,
                 cooldown: float = config.PENALTY_403_COOLDOWN_S,
                 stand_down: bool = True, proxies: dict | None = None,
                 pool_size: int = 10):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = config.USER_AGENT
        if proxies:
            from requests.adapters import HTTPAdapter
            adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size,
                                  max_retries=0)
            self.s.mount("http://", adapter)
            self.s.mount("https://", adapter)
        self.proxies = proxies
        self.min_interval = min_interval
        self.cooldown = cooldown
        self.stand_down = stand_down
        self._last = 0.0
        self.calls = 0
        self.n_429 = 0
        self.n_403 = 0

    # ------------------------------------------------------------------ transport
    def _pace(self) -> None:
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)

    def _get(self, url: str, params: dict) -> dict:
        for attempt in range(config.MAX_RETRIES + 1):
            self._pace()
            try:
                r = self.s.get(url, params=params, timeout=60, proxies=self.proxies)
            except requests.RequestException as e:
                log.warning("network error %s; retry %d", e, attempt)
                time.sleep(5 * (attempt + 1))
                self._last = time.time()
                continue
            self._last = time.time()
            self.calls += 1

            if r.status_code == 200:
                # Apple serves JSON as text/javascript with leading whitespace.
                try:
                    return r.json()
                except ValueError:
                    import json
                    return json.loads(r.text.strip())

            if r.status_code == 429:
                self.n_429 += 1
                backoff = config.RETRY_429_BACKOFF_S[
                    min(attempt, len(config.RETRY_429_BACKOFF_S) - 1)]
                log.warning("429 -- backing off %ss (attempt %d)", backoff, attempt)
                time.sleep(backoff)
                continue

            if r.status_code == 403:
                self.n_403 += 1
                if not self.stand_down:
                    raise RateLimitPenalty("403 from Apple")
                log.error("403 penalty box -- standing down %ss. Short retries make "
                          "this worse, they do not fix it.", self.cooldown)
                time.sleep(self.cooldown)
                continue

            log.warning("HTTP %s on %s", r.status_code, url)
            time.sleep(5 * (attempt + 1))

        raise RateLimitPenalty(
            f"gave up after {config.MAX_RETRIES} retries on {url}")

    # ------------------------------------------------------------------ endpoints
    def search(self, term: str) -> tuple[list[dict], bool]:
        """Return (results, truncated).

        `truncated` means the query hit the ~190 ceiling and is hiding results.
        There is no pagination to fall back on -- the caller must narrow the term.
        """
        data = self._get(config.ITUNES_SEARCH, {
            "term": term,
            "entity": "software",
            "country": config.COUNTRY,
            "limit": config.SEARCH_LIMIT,
        })
        results = data.get("results", []) or []
        return results, len(results) >= config.SEARCH_CEILING

    def lookup_batch(self, track_ids: Iterable[int],
                     check_truncation: bool = True) -> list[dict]:
        """Look up <= 200 ids. Asserts the response length: over 200 Apple silently
        truncates to ~210 rows behind an HTTP 200.

        check_truncation=False skips the >50%-missing-means-truncation guard below.
        Needed for a retry pass over a batch that's ALREADY been pre-filtered down
        to ids missing from a prior pass (enrich.refresh_known_apps()) -- there, a
        high (even 100%) still-missing fraction is the expected, normal outcome
        for a batch of mostly-genuinely-delisted apps, not evidence of truncation.
        Found 2026-09-11: a retry batch of 84 previously-missing ids came back
        0/84 and tripped this guard, crashing the whole run.
        """
        ids = list(track_ids)
        if not ids:
            return []
        if len(ids) > config.LOOKUP_BATCH_SIZE:
            raise ValueError(
                f"batch of {len(ids)} exceeds the measured safe size of "
                f"{config.LOOKUP_BATCH_SIZE}; Apple truncates silently above it")

        data = self._get(config.ITUNES_LOOKUP, {
            "id": ",".join(str(i) for i in ids),
            "country": config.COUNTRY,
        })
        rows = [r for r in data.get("results", []) if r.get("wrapperType") == "software"]

        returned = {r.get("trackId") for r in rows}
        missing = [i for i in ids if i not in returned]
        if missing:
            # Legitimately missing ids (delisted apps) are expected and are how we
            # detect delisting. A *large* shortfall means truncation, not delisting
            # -- but only on a batch drawn from the general corpus; see docstring.
            if check_truncation and len(missing) > len(ids) * 0.5:
                raise RuntimeError(
                    f"lookup returned {len(rows)}/{len(ids)} rows -- looks like silent "
                    f"truncation, not delisting")
            log.info("lookup: %d/%d ids returned; %d absent (delisted?)",
                     len(rows), len(ids), len(missing))
        return rows

    def lookup_many(self, track_ids: Iterable[int],
                    check_truncation: bool = True) -> Iterator[list[dict]]:
        """Chunk into safe batches and yield each batch's rows."""
        ids = list(track_ids)
        for i in range(0, len(ids), config.LOOKUP_BATCH_SIZE):
            yield self.lookup_batch(ids[i:i + config.LOOKUP_BATCH_SIZE],
                                    check_truncation=check_truncation)

    def artist_apps(self, artist_id: int) -> list[dict]:
        """Every app under a developer account.

        High-yield for vendor_account publishers (Chronogolf: 222 apps under one
        account). Near-useless for operator_account ones -- Sagacity's own account
        holds 24 apps and only 11 of its 29 known courses.
        """
        data = self._get(config.ITUNES_LOOKUP, {
            "id": artist_id,
            "entity": "software",
            "limit": config.SEARCH_LIMIT,
            "country": config.COUNTRY,
        })
        return [r for r in data.get("results", []) if r.get("wrapperType") == "software"]


def normalize_query(term: str) -> str:
    """Canonical cache key. Queries are never issued twice, across runs or months."""
    return " ".join(term.lower().split())


def app_row(r: dict) -> dict:
    """iTunes JSON -> ga_app row. HTML-only fields stay null until enrich."""
    return {
        "track_id": r.get("trackId"),
        "store": config.STORE,
        "bundle_id": r.get("bundleId"),
        "track_name": r.get("trackName"),
        "seller_name": r.get("sellerName"),
        "artist_id": r.get("artistId"),
        "artist_name": r.get("artistName"),
        "seller_url": r.get("sellerUrl"),
        "artwork_url": r.get("artworkUrl512") or r.get("artworkUrl100"),
        "description": r.get("description"),
        "primary_genre": r.get("primaryGenreName"),
        "release_date": r.get("releaseDate"),
        "current_version_release_date": r.get("currentVersionReleaseDate"),
    }
