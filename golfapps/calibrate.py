"""Calibration -- measure recall before spending the crawl budget.

Reports, per vendor: how many known courses were found, WHICH mechanism caught each
one, and -- the line that actually matters -- which known courses were missed and
which field would have caught them.

Ground truth comes from two places:
  * data/ground_truth.csv    (facility_name, vendor) -- hand-curated
  * matchservice2.0/whoosh.csv and lightspeed3.csv -- client lists that already exist
"""
from __future__ import annotations

import csv
import logging
from collections import defaultdict
from pathlib import Path

from . import db, itunes, vendors

log = logging.getLogger(__name__)

GROUND_TRUTH_CSV = Path(__file__).parents[1] / "data" / "ground_truth.csv"

# Vendor CLIENT lists are not the same thing as lists of facilities that have an app.
# A course can run Chronogolf's tee sheet and ship no app at all. Scoring app recall
# against a customer list therefore understates the pipeline badly -- these are kept,
# but reported as coverage, not as recall.
EXISTING_LISTS = {
    "whoosh": (Path(r"C:\Downgrain\matchservice2.0\whoosh.csv"), "customer_list"),
    "chronogolf": (Path(r"C:\Downgrain\matchservice2.0\lightspeed3.csv"), "customer_list"),
}


def load_ground_truth() -> list[dict]:
    truth: list[dict] = []
    if GROUND_TRUTH_CSV.exists():
        with GROUND_TRUTH_CSV.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("facility_name") and row.get("vendor"):
                    truth.append({"facility_name": row["facility_name"].strip(),
                                  "vendor": row["vendor"].strip().lower(),
                                  "source": "ground_truth.csv",
                                  "kind": "app_truth"})
    for vendor, (path, kind) in EXISTING_LISTS.items():
        if not path.exists():
            continue
        with path.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                name = (row.get("facility_name") or "").strip()
                if name:
                    truth.append({"facility_name": name, "vendor": vendor,
                                  "source": path.name, "kind": kind})
    return truth


def _corpus_by_name() -> dict[str, list[dict]]:
    rows = db.query("""
        SELECT a.track_id, a.track_name, a.bundle_id, a.seller_name, a.artist_name,
               a.seller_url, a.copyright, a.privacy_policy_url, a.support_url,
               a.developer_website, a.description, v.vendor, v.confidence,
               v.matched_fields
        FROM ga_app a LEFT JOIN ga_app_vendor v ON v.track_id = a.track_id
        WHERE a.delisted_at IS NULL
    """)
    idx: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        for key in _name_keys(r.get("track_name") or ""):
            idx[key].append(r)
    return idx


_STOP = {"golf", "club", "course", "resort", "country", "links", "the", "at", "and",
         "gc", "cc", "tee", "times", "app", "mobile", "members", "member"}


def _name_keys(name: str) -> set[str]:
    """Keys strict enough that a match means something.

    An earlier version also keyed on the first significant token, which made
    'North Shore Country Club' collide with every app starting 'North ...' and
    produced confident-looking nonsense. Require the full significant-token set,
    order-insensitively.
    """
    toks = [t for t in "".join(c.lower() if c.isalnum() else " " for c in name).split()
            if t and t not in _STOP]
    if len(toks) < 1:
        return set()
    return {" ".join(toks), " ".join(sorted(toks))}


def run(quiet: bool = False) -> dict:
    truth = load_ground_truth()
    if not truth:
        return {"error": f"no ground truth found; create {GROUND_TRUTH_CSV}"}

    idx = _corpus_by_name()
    per_vendor: dict[str, dict] = defaultdict(
        lambda: {"known": 0, "found": 0, "correct": 0, "missed": [],
                 "wrong_vendor": [], "by_mechanism": defaultdict(int),
                 "kinds": defaultdict(int)})

    for t in truth:
        v = t["vendor"]
        rec = per_vendor[v]
        rec["known"] += 1
        rec["kinds"][t.get("kind", "app_truth")] += 1

        hits: list[dict] = []
        for key in _name_keys(t["facility_name"]):
            hits.extend(idx.get(key, []))
        if not hits:
            rec["missed"].append({"facility_name": t["facility_name"],
                                  "reason": "not in corpus",
                                  "would_have_been_caught_by": "sweep coverage"})
            continue

        rec["found"] += 1
        best = max(hits, key=lambda h: {"high": 3, "medium": 2, "low": 1}.get(
            h.get("confidence") or "", 0))
        if (best.get("vendor") or "") == v:
            rec["correct"] += 1
            fields = best.get("matched_fields") or []
            mech = fields[0]["field"] if fields else "unknown"
            rec["by_mechanism"][mech] += 1
        else:
            # Found the app but attributed it elsewhere -- name the field that WOULD
            # have caught it. This is the line that tells you where the gap is.
            would = _which_field_would_catch(best, v)
            rec["wrong_vendor"].append({
                "facility_name": t["facility_name"],
                "got": best.get("vendor"), "want": v,
                "track_id": best["track_id"],
                "would_have_been_caught_by": would,
            })

    reg = vendors.load()
    undetectable = set(vendors.undetectable_vendors(reg))

    out = {"vendors": {}, "totals": {"known": 0, "found": 0, "correct": 0},
           "excluded": {"undetectable": {}, "customer_lists": {}}}
    for v, rec in sorted(per_vendor.items()):
        recall = round(rec["correct"] / rec["known"] * 100, 1) if rec["known"] else 0.0
        attribution = (round(rec["correct"] / rec["found"] * 100, 1)
                       if rec["found"] else 0.0)
        entry = {
            "known": rec["known"], "found_in_corpus": rec["found"],
            "correctly_labelled": rec["correct"], "recall_pct": recall,
            "attribution_accuracy_pct": attribution,
            "kinds": dict(rec["kinds"]),
            "by_mechanism": dict(rec["by_mechanism"]),
            "missed": rec["missed"][:25],
            "wrong_vendor": rec["wrong_vendor"][:25],
        }
        out["vendors"][v] = entry

        # A vendor with no per-facility listing cannot be found by this pipeline, and
        # a customer list does not assert that an app exists. Neither belongs in the
        # headline recall -- averaging them in would measure the wrong thing.
        if v in undetectable:
            out["excluded"]["undetectable"][v] = entry
            continue
        if rec["kinds"].get("customer_list", 0) > rec["kinds"].get("app_truth", 0):
            out["excluded"]["customer_lists"][v] = entry
            continue

        out["totals"]["known"] += rec["known"]
        out["totals"]["found"] += rec["found"]
        out["totals"]["correct"] += rec["correct"]

    t = out["totals"]
    t["recall_pct"] = round(t["correct"] / t["known"] * 100, 1) if t["known"] else 0.0
    t["attribution_accuracy_pct"] = (round(t["correct"] / t["found"] * 100, 1)
                                     if t["found"] else 0.0)
    out["verdict"] = _verdict(out)
    if not quiet:
        print(render(out))
    return out


def _which_field_would_catch(app: dict, want_vendor: str) -> str:
    """Given the app row, which field names the vendor we expected?"""
    reg = vendors.load()
    v = reg.vendors.get(want_vendor)
    if not v:
        return f"vendor '{want_vendor}' is not in vendors.yaml"
    hits = vendors._hits_for(v, app)
    if hits:
        return (f"{sorted(hits)} already match but lost on authority -- "
                f"check field_authority")
    if app.get("copyright") is None:
        return "copyright (not fetched -- app never escalated to HTML)"
    return "no field on this listing names that vendor"


def _verdict(out: dict) -> str:
    r = out["totals"]["recall_pct"]
    if r >= 90:
        return f"PASS -- {r}% overall recall meets the >90% target."
    if r >= 70:
        return (f"MARGINAL -- {r}% recall. Above the 70% stop line but below target; "
                f"close the gaps listed under 'would_have_been_caught_by' first.")
    return (f"STOP -- {r}% recall is below the 70% line. The plan says reconsider "
            f"rather than scale up. Do not spend the full crawl budget yet.")


def render(out: dict) -> str:
    if "error" in out:
        return f"calibration: {out['error']}"
    lines = ["=" * 72, "CALIBRATION -- recall against known facility->vendor pairs",
             "=" * 72, ""]
    excluded = set(out["excluded"]["undetectable"]) | set(out["excluded"]["customer_lists"])
    lines.append(f"  {'vendor':18s} {'known':>6s} {'found':>6s} {'correct':>8s} "
                 f"{'recall':>8s} {'attrib':>8s}   mechanisms")
    for v, r in out["vendors"].items():
        if v in excluded:
            continue
        mech = ", ".join(f"{k}:{n}" for k, n in
                         sorted(r["by_mechanism"].items(), key=lambda kv: -kv[1])[:3])
        lines.append(f"  {v:18s} {r['known']:>6d} {r['found_in_corpus']:>6d} "
                     f"{r['correctly_labelled']:>8d} {r['recall_pct']:>7.1f}% "
                     f"{r['attribution_accuracy_pct']:>7.1f}%   {mech}")
    t = out["totals"]
    lines += ["",
              f"  RECALL      {t['correct']}/{t['known']} = {t['recall_pct']}%"
              f"   (found the app AND labelled it correctly)",
              f"  ATTRIBUTION {t['correct']}/{t['found']} = "
              f"{t['attribution_accuracy_pct']}%"
              f"   (of apps found, labelled correctly)",
              "", out["verdict"], ""]

    if excluded:
        lines.append("EXCLUDED from the headline (counting these would measure the "
                     "wrong thing):")
        for v, r in out["excluded"]["undetectable"].items():
            lines.append(f"  {v:18s} {r['known']:>5d} known, {r['found_in_corpus']:>4d} "
                         f"found -- ships ONE multi-tenant app; no per-facility "
                         f"listing exists to find")
        for v, r in out["excluded"]["customer_lists"].items():
            lines.append(f"  {v:18s} {r['known']:>5d} known, {r['found_in_corpus']:>4d} "
                         f"found ({r['attribution_accuracy_pct']}% attribution) -- "
                         f"source is a CUSTOMER list; those courses need not have apps")
        lines.append("")

    lines.append("MISSES -- and the field that would have caught each:")
    any_miss = False
    for v, r in out["vendors"].items():
        for m in r["missed"][:8]:
            any_miss = True
            lines.append(f"  [{v}] {m['facility_name'][:44]:46s} "
                         f"-> {m['would_have_been_caught_by']}")
        for w in r["wrong_vendor"][:8]:
            any_miss = True
            lines.append(f"  [{v}] {w['facility_name'][:44]:46s} "
                         f"-> labelled {w['got']}; {w['would_have_been_caught_by']}")
    if not any_miss:
        lines.append("  (none)")
    return "\n".join(lines)
