"""Vendor fingerprint matching and label assignment.

The whole point of the authority ranking is that fields inside a single listing
routinely disagree, and the disagreement is meaningful:

  * Teesnap sells the Gallus product. 27 apps have bundleId com.gallusgolf.* and
    sellerName 'Teesnap, LLC'. seller_name wins -> Teesnap.
  * Los Serranos migrated Quick18 -> Gallus. copyright and privacy say Gallus;
    seller_url still says quick18.com. copyright wins -> Gallus, and the conflict
    is recorded because a mid-flight vendor switch is exactly what the monthly
    diff is meant to surface.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import yaml

from . import config

# Fields we scan, mapped to the app row keys they come from.
SCAN_FIELDS = [
    "seller_name", "artist_name", "copyright", "privacy_policy_url", "support_url",
    "bundle_id", "seller_url", "developer_website", "track_name", "description",
]


@dataclass
class VendorConfig:
    key: str
    display_name: str
    category: str
    publishing_model: str
    tokens: list[str] = field(default_factory=list)
    bundle_prefixes: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    copyright_patterns: list[str] = field(default_factory=list)
    seller_names: list[str] = field(default_factory=list)
    known_artist_ids: list[int] = field(default_factory=list)
    exclude_tokens: list[str] = field(default_factory=list)
    exclude_domains: list[str] = field(default_factory=list)
    exclude_bundle_prefixes: list[str] = field(default_factory=list)
    detectable_per_facility: bool = True
    parent: str | None = None
    evidence: str = ""


@dataclass
class Registry:
    vendors: dict[str, VendorConfig]
    authority: dict[str, int]
    tiers: dict[str, int]

    def tier_for(self, authority: int) -> str:
        if authority >= self.tiers["high"]:
            return "high"
        if authority >= self.tiers["medium"]:
            return "medium"
        if authority >= self.tiers["low"]:
            return "low"
        return "unknown"


@lru_cache(maxsize=1)
def load(path: str | None = None) -> Registry:
    raw = yaml.safe_load((config.VENDORS_YAML if path is None
                          else __import__("pathlib").Path(path)).read_text("utf-8"))
    vendors = {}
    for key, cfg in (raw.get("vendors") or {}).items():
        vendors[key] = VendorConfig(
            key=key,
            display_name=cfg.get("display_name", key),
            category=cfg.get("category", "booking"),
            publishing_model=cfg.get("publishing_model", "operator_account"),
            tokens=[t.lower() for t in cfg.get("tokens", [])],
            bundle_prefixes=[p.lower() for p in cfg.get("bundle_prefixes", [])],
            domains=[d.lower() for d in cfg.get("domains", [])],
            copyright_patterns=cfg.get("copyright_patterns", []),
            seller_names=cfg.get("seller_names", []),
            known_artist_ids=cfg.get("known_artist_ids", []),
            exclude_tokens=[t.lower() for t in cfg.get("exclude_tokens", [])],
            exclude_domains=[d.lower() for d in cfg.get("exclude_domains", [])],
            exclude_bundle_prefixes=[p.lower() for p in
                                     cfg.get("exclude_bundle_prefixes", [])],
            detectable_per_facility=cfg.get("detectable_per_facility", True),
            parent=cfg.get("parent"),
            evidence=cfg.get("evidence", ""),
        )
    return Registry(vendors=vendors,
                    authority=raw["field_authority"],
                    tiers=raw["confidence_tiers"])


def _host(url: str | None) -> str:
    if not url:
        return ""
    m = re.match(r"https?://([^/]+)", url.strip(), re.I)
    h = (m.group(1) if m else url).lower()
    return h[4:] if h.startswith("www.") else h


def _excluded(v: VendorConfig, row: dict) -> bool:
    bundle = (row.get("bundle_id") or "").lower()
    if any(bundle.startswith(p) for p in v.exclude_bundle_prefixes):
        return True
    for f in ("seller_url", "developer_website", "privacy_policy_url", "support_url"):
        h = _host(row.get(f))
        if h and any(h == d or h.endswith("." + d) for d in v.exclude_domains):
            return True
    blob = " ".join(str(row.get(f) or "") for f in SCAN_FIELDS).lower()
    return any(t in blob for t in v.exclude_tokens)


def _hits_for(v: VendorConfig, row: dict) -> dict[str, str]:
    """Return {field: reason} for every field of `row` naming vendor `v`."""
    hits: dict[str, str] = {}
    if _excluded(v, row):
        return hits

    bundle = (row.get("bundle_id") or "").lower()
    if bundle and any(bundle.startswith(p) for p in v.bundle_prefixes):
        hits["bundle_id"] = "bundle_prefix"

    for f in ("seller_url", "developer_website", "privacy_policy_url", "support_url"):
        h = _host(row.get(f))
        if h and any(h == d or h.endswith("." + d) for d in v.domains):
            hits[f] = "domain"

    # Copyright falls back to plain tokens. Most vendors have no curated
    # copyright_patterns, and without this fallback their name being *literally in the
    # copyright line* counts for nothing: Scarsdale reads "(c) 2026 MembersFirst" but
    # was attributed to Jonas, because only Jonas' privacy-policy domain could match.
    # Copyright outranks privacy_policy_url precisely because it is the more specific
    # claim, so it has to be checked properly.
    cp = row.get("copyright") or ""
    if cp:
        cpl = cp.lower()
        if any(p.lower() in cpl for p in v.copyright_patterns):
            hits["copyright"] = "copyright_pattern"
        elif any(t in cpl for t in v.tokens):
            hits["copyright"] = "token"

    for f in ("seller_name", "artist_name"):
        val = (row.get(f) or "").lower()
        if not val:
            continue
        if any(sn.lower() in val for sn in v.seller_names):
            hits[f] = "seller_name"
        elif any(t in val for t in v.tokens):
            hits[f] = "token"

    if row.get("artist_id") and row["artist_id"] in v.known_artist_ids:
        hits["artist_name"] = "known_artist_id"

    for f in ("track_name", "description"):
        val = (row.get(f) or "").lower()
        if val and any(t in val for t in v.tokens):
            hits.setdefault(f, "token")

    # A bare token anywhere else still counts, at that field's authority.
    for f in ("bundle_id", "seller_url", "privacy_policy_url", "support_url",
              "developer_website"):
        if f in hits:
            continue
        val = (row.get(f) or "").lower()
        if val and any(t in val for t in v.tokens):
            hits[f] = "token"

    return hits


@dataclass
class Label:
    vendor: str | None
    confidence: str
    matched_fields: list[dict]
    conflict: bool = False
    conflict_detail: list[dict] | None = None

    def as_row(self, track_id: int) -> dict:
        return {
            "track_id": track_id,
            "vendor": self.vendor,
            "confidence": self.confidence,
            "matched_fields": self.matched_fields,
            "conflict": self.conflict,
            "conflict_detail": self.conflict_detail,
        }


def label_app(row: dict, reg: Registry | None = None) -> Label:
    """Assign a vendor to one app row, resolving field disagreement by authority."""
    reg = reg or load()
    scored: list[tuple[int, str, dict[str, str]]] = []

    for key, v in reg.vendors.items():
        hits = _hits_for(v, row)
        if not hits:
            continue
        best = max(reg.authority.get(f, 0) for f in hits)
        scored.append((best, key, hits))

    if not scored:
        return Label(vendor=None, confidence="unknown", matched_fields=[])

    scored.sort(key=lambda t: -t[0])
    top_authority, winner, winner_hits = scored[0]

    # Parent/child: a child vendor is strictly more specific than its parent, and it
    # only ever wins on a lower-authority field. ClubHouse Online shares Jonas' bundle
    # prefix entirely -- its seller_url domain is the ONLY thing that separates them,
    # so letting bundle_id outrank it would erase the distinction on every app.
    for authority, key, hits in scored[1:]:
        if reg.vendors[key].parent == winner:
            top_authority, winner, winner_hits = authority, key, hits
            break

    matched = [{"vendor": winner, "field": f, "reason": r,
                "authority": reg.authority.get(f, 0)}
               for f, r in sorted(winner_hits.items(),
                                  key=lambda kv: -reg.authority.get(kv[0], 0))]

    # Distinct vendors named by the same listing. Keep the parent/child pair
    # (ClubHouse Online under Jonas) out of the conflict count -- that is by design.
    rivals = []
    for authority, key, hits in scored:
        if key == winner:
            continue          # promotion can move the winner out of position 0
        w, k = reg.vendors[winner], reg.vendors[key]
        if w.parent == key or k.parent == winner:
            continue          # parent/child is by design, not a disagreement
        rivals.append({"vendor": key, "authority": authority,
                       "fields": sorted(hits)})

    confidence = reg.tier_for(top_authority)
    # A description-only match identifies a codebase lineage, not a vendor. The
    # Quick18 template appears verbatim on a Gallus-owned listing.
    if set(winner_hits) <= {"description", "track_name"}:
        confidence = "low"

    return Label(vendor=winner, confidence=confidence, matched_fields=matched,
                 conflict=bool(rivals), conflict_detail=rivals or None)


def label_rows(rows: list[dict]) -> list[Label]:
    reg = load()
    return [label_app(r, reg) for r in rows]


# ---------------------------------------------------------------- helpers
BOOKING_RE = re.compile(
    r"(?i)(tee time|book .{0,25}tee|tee sheet|golf reservation|"
    r"member (portal|app)|club app)")
COURSE_RE = re.compile(
    r"(?i)(provides tee time booking|book(ing)? tee times? (for|at)|"
    r"golf (course|club|resort)|country club)")


def looks_like_course_app(row: dict) -> bool:
    """Cheap gate before we spend an HTML fetch on an app."""
    blob = f"{row.get('track_name') or ''} {row.get('description') or ''}"
    return bool(BOOKING_RE.search(blob) and COURSE_RE.search(blob))


def undetectable_vendors(reg: Registry | None = None) -> list[str]:
    """Vendors that ship one multi-tenant app, so no per-facility listing exists.
    Reported explicitly rather than shown as zero market share."""
    reg = reg or load()
    return [k for k, v in reg.vendors.items() if not v.detectable_per_facility]
