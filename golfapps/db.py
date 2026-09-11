"""Supabase access.

Reads go through the `query_sql` RPC (SELECT-only, service_role only, no bind
parameters -- interpolate via lit()). Writes go through PostgREST so we get upsert
semantics. Same pattern as facility_adder/db.py.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable, Sequence

import requests

from . import config

_TIMEOUT = 120


def _headers() -> dict[str, str]:
    config.require_credentials()
    return {
        "apikey": config.SUPABASE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_KEY}",
        "Content-Type": "application/json",
    }


def lit(value: Any) -> str:
    """SQL literal. query_sql takes no bind parameters, so this is the only safe path."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        return "'" + json.dumps(value).replace("'", "''") + "'::jsonb"
    return "'" + str(value).replace("'", "''") + "'"


def query(sql: str) -> list[dict]:
    """Run a SELECT through the query_sql RPC."""
    r = requests.post(
        f"{config.SUPABASE_URL}/rest/v1/rpc/query_sql",
        headers=_headers(),
        json={"query": sql},
        timeout=_TIMEOUT,
    )
    if r.status_code != 200:
        raise RuntimeError(f"query_sql failed {r.status_code}: {r.text[:500]}\nSQL: {sql[:300]}")
    return r.json() or []


def exec_sql(sql: str) -> None:
    """Run a statement that returns nothing (DDL, DELETE)."""
    r = requests.post(
        f"{config.SUPABASE_URL}/rest/v1/rpc/exec_sql",
        headers=_headers(),
        json={"query": sql},
        timeout=_TIMEOUT,
    )
    if r.status_code not in (200, 204):
        raise RuntimeError(f"exec_sql failed {r.status_code}: {r.text[:500]}")


MAX_DESCRIPTION_CHARS = 4000


def _trim(rows: Sequence[dict]) -> list[dict]:
    """App descriptions run to several KB. A 500-row batch of them is megabytes,
    which the edge proxy rejects with a 502. We only ever parse the first part of a
    description (course name and city), so cap it."""
    out = []
    for r in rows:
        if r.get("description") and len(r["description"]) > MAX_DESCRIPTION_CHARS:
            r = dict(r, description=r["description"][:MAX_DESCRIPTION_CHARS])
        out.append(r)
    return out


def upsert(table: str, rows: Sequence[dict], on_conflict: str,
           chunk: int = 100, retries: int = 4) -> int:
    """PostgREST bulk upsert, chunked and retried. Returns rows sent."""
    if not rows:
        return 0
    import time

    h = _headers() | {"Prefer": "resolution=merge-duplicates,return=minimal"}
    payload = _trim(rows)
    sent = 0
    for i in range(0, len(payload), chunk):
        batch = payload[i:i + chunk]
        for attempt in range(retries):
            try:
                r = requests.post(
                    f"{config.SUPABASE_URL}/rest/v1/{table}",
                    headers=h,
                    params={"on_conflict": on_conflict},
                    json=batch,
                    timeout=_TIMEOUT,
                )
            except requests.RequestException as e:
                if attempt == retries - 1:
                    raise RuntimeError(f"upsert {table} network error: {e}") from e
                time.sleep(3 * (attempt + 1))
                continue

            if r.status_code in (200, 201, 204):
                break
            # 5xx here is usually payload size or a transient edge error.
            if r.status_code >= 500 and attempt < retries - 1:
                if len(batch) > 10:
                    half = len(batch) // 2
                    sent += upsert(table, batch[:half], on_conflict, chunk=half)
                    sent += upsert(table, batch[half:], on_conflict, chunk=half)
                    batch = []
                    break
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"upsert {table} failed {r.status_code}: {r.text[:400]}")
        sent += len(batch)
    return sent


def split_statements(sql: str) -> list[str]:
    """Split a DDL script into statements.

    Comment lines are stripped BEFORE splitting: a statement preceded by a comment
    block would otherwise look like a comment and be dropped, which silently skips
    the CREATE TABLE and leaves its CREATE INDEX to fail.

    Dollar-quote aware (`$$ ... $$` / `$tag$ ... $tag$`): a `;` inside a plpgsql
    function body must not split the function in two. Added 2026-09-11 alongside
    schema.sql's first CREATE FUNCTION (resolve_review_item) -- the naive
    split-on-";" that worked for every prior statement here would have cut that
    function's body into several syntactically invalid fragments.
    """
    lines = [ln for ln in sql.splitlines() if not ln.lstrip().startswith("--")]
    body = "\n".join(lines)

    statements: list[str] = []
    buf: list[str] = []
    dollar_tag: str | None = None
    i, n = 0, len(body)
    while i < n:
        if dollar_tag is None:
            m = re.match(r"\$[A-Za-z_]*\$", body[i:])
            if m:
                dollar_tag = m.group(0)
                buf.append(dollar_tag)
                i += len(dollar_tag)
                continue
            if body[i] == ";":
                stmt = "".join(buf).strip()
                if stmt:
                    statements.append(stmt)
                buf = []
                i += 1
                continue
            buf.append(body[i])
            i += 1
        else:
            if body[i:i + len(dollar_tag)] == dollar_tag:
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(body[i])
            i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def init_schema() -> None:
    """Apply schema.sql. Idempotent -- every statement is IF NOT EXISTS."""
    for st in split_statements(config.SCHEMA_SQL.read_text(encoding="utf-8")):
        exec_sql(st)


# ---------------------------------------------------------------- domain reads
def fetch_facilities(limit: int | None = None) -> list[dict]:
    """The penetration denominator: 13,922 US Golf Course rows, Active or Pending."""
    sql = f"""
        SELECT facility_id, facility_name, city, state_code, zip, website_url, domain,
               facility_accessibility, hole_count
        FROM facility
        WHERE {config.FACILITY_WHERE}
        ORDER BY facility_id
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return query(sql)


def facility_count() -> int:
    return query(f"SELECT count(*) n FROM facility WHERE {config.FACILITY_WHERE}")[0]["n"]


def cached_queries() -> set[str]:
    return {r["query_normalized"] for r in query("SELECT query_normalized FROM ga_search_cache")}


def known_track_ids() -> set[int]:
    return {r["track_id"] for r in query("SELECT track_id FROM ga_app")}


def tracked_track_ids() -> set[int]:
    """Apps worth a monthly liveness/metadata refresh: crosswalk-linked to an
    actual facility/owner (what the delisted-apps report tracks -- Derek,
    2026-09-11) plus vendor-confirmed (what the matching waterfall and review
    queue draw from).

    Deliberately excludes two things:
      * the much larger pool of generic-sweep padding that never got a vendor
        label and never feeds anything -- refreshing those every run wastes
        API calls checking apps nobody is tracking (measured: 16,356 in the
        full corpus vs. ~5,000 actually relevant);
      * crosswalk rows with link_type='exclude' (not_a_golf_course, non-US)
        -- Derek, 2026-09-11: "we only care about good links... if something
        that is not good was delisted, we don't care." An excluded app isn't
        believed to be anyone's golf-course app; its liveness is noise. Many
        excluded apps ARE vendor-confirmed (e.g. a vendor's own non-golf
        product, like Jonas Construction Tools), so this has to be an
        explicit exclusion on top of the vendor-confirmed union, not just a
        filter on which crosswalk rows qualify for inclusion -- otherwise
        they leak back in through the vendor-confirmed half (measured:
        1,490 of them did, all 1,490 of the current exclude rows).
    """
    return {r["track_id"] for r in query("""
        SELECT track_id FROM (
            SELECT track_id FROM app_crosswalk WHERE link_type IN ('facility', 'owner')
            UNION
            SELECT track_id FROM ga_app_vendor WHERE vendor IS NOT NULL
        ) t
        WHERE t.track_id NOT IN (
            SELECT track_id FROM app_crosswalk WHERE link_type = 'exclude'
        )
    """)}


def apps_missing_html() -> list[dict]:
    return query("""
        SELECT a.track_id, a.bundle_id, a.seller_url, a.seller_name, a.artist_name
        FROM ga_app a
        LEFT JOIN ga_app_vendor v ON v.track_id = a.track_id
        WHERE a.html_fetched_at IS NULL
          AND (v.vendor IS NULL OR v.confidence IN ('low', 'unknown'))
        ORDER BY a.track_id
    """)
