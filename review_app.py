"""Local review queue UI -- a faster alternative to hand-writing SQL while
Retool isn't wired up yet, and a fine tool to keep using afterward for quick
local sessions. Writes through the exact same resolve_review_item() Postgres
function Retool will call, so there's no separate code path to drift out of
sync with it.

Run from the repo root:
    pip install -e ".[dev]"
    streamlit run review_app.py
"""
from __future__ import annotations

import streamlit as st

from golfapps import db, join, report

st.set_page_config(page_title="Golf App Review Queue", layout="wide")


# ---------------------------------------------------------------- data access
@st.cache_data(ttl=15)
def load_pending() -> list[dict]:
    return db.query("""
        SELECT track_id, app_name, vendor, confidence, bundle_id, seller_name,
               subtitle, app_store_url, description, candidates, owner_suggestion
        FROM ga_review_queue
        WHERE status = 'pending'
        ORDER BY app_name
    """)


def search_facilities(term: str) -> list[dict]:
    if not term or len(term) < 2:
        return []
    safe = term.replace("'", "''")
    return db.query(f"""
        SELECT facility_id, facility_name, city, state_code
        FROM facility
        WHERE facility_name ILIKE '%{safe}%'
        ORDER BY facility_name
        LIMIT 15
    """)


def resolve(track_id: int, decision: str, facility_ids: list[int] | None = None,
           owner_id: int | None = None, exclude_reason: str | None = None,
           country: str | None = None) -> None:
    fac_sql = ("ARRAY[" + ",".join(str(f) for f in facility_ids) + "]::bigint[]"
              if facility_ids else "NULL")
    db.exec_sql(f"""
        SELECT resolve_review_item(
            {track_id}, {db.lit(decision)}, {fac_sql},
            {owner_id if owner_id else 'NULL'},
            {db.lit(exclude_reason) if exclude_reason else 'NULL'},
            {db.lit(country) if country else 'NULL'},
            'streamlit:derek')
    """)
    load_pending.clear()


# ---------------------------------------------------------------- sidebar
pending = load_pending()
st.sidebar.title("Review queue")
st.sidebar.metric("Pending", len(pending))

st.sidebar.divider()
st.sidebar.caption(
    "Sync re-applies the FULL current crosswalk table and refreshes this "
    "month's snapshot -- cheap and idempotent, not scoped to a time window, "
    "so nothing gets missed if you go a while between syncs."
)
if st.sidebar.button("Sync resolved decisions to snapshot", type="primary"):
    with st.sidebar:
        with st.spinner("Applying crosswalk + writing snapshot..."):
            cw_stats = join.apply_crosswalk()
            snap = report.write_snapshot()
    st.sidebar.success(f"Snapshot {snap['run_id']}: {snap['rows']:,} rows")

recent = db.query("""
    SELECT track_id, link_type, source, updated_at
    FROM app_crosswalk
    WHERE updated_at >= now() - interval '7 days'
    ORDER BY updated_at DESC
    LIMIT 100
""")
with st.sidebar.expander(f"Resolved in the last 7 days ({len(recent)})"):
    for r in recent:
        st.write(f"{r['track_id']} -- {r['link_type']} ({r['source']})")


# ---------------------------------------------------------------- main review flow
st.title("Golf App Review Queue")

if "idx" not in st.session_state:
    st.session_state.idx = 0

if not pending:
    st.success("Queue is empty.")
    st.stop()

st.session_state.idx = max(0, min(st.session_state.idx, len(pending) - 1))
idx = st.session_state.idx
item = pending[idx]

nav1, nav2, nav3 = st.columns([1, 1, 6])
if nav1.button("< Prev", disabled=idx == 0):
    st.session_state.idx -= 1
    st.rerun()
if nav2.button("Skip >"):
    st.session_state.idx = min(idx + 1, len(pending) - 1)
    st.rerun()
st.caption(f"Item {idx + 1} of {len(pending)}")

left, right = st.columns([3, 2])

with left:
    st.subheader(item["app_name"] or "(no name)")
    st.markdown(f"[Open in App Store]({item['app_store_url']})")
    st.write(f"**Vendor:** {item['vendor']} ({item['confidence']})")
    st.write(f"**Bundle ID:** {item['bundle_id'] or '-'}")
    st.write(f"**Seller:** {item['seller_name'] or '-'}")
    st.write(f"**Subtitle:** {item['subtitle'] or '-'}")
    st.text_area("Description", item["description"] or "", height=120, disabled=True)

with right:
    st.subheader("Resolve")

    candidates = item["candidates"] or []
    if candidates:
        st.write("**Suggested candidates:**")
        for c in candidates:
            label = f"{c['facility_name']} -- {c.get('city') or '?'}, {c.get('state_code') or '?'} (id {c['facility_id']})"
            if st.button(f"Link to: {label}", key=f"cand_{item['track_id']}_{c['facility_id']}"):
                resolve(item["track_id"], "facility", facility_ids=[c["facility_id"]])
                st.rerun()

    if item["owner_suggestion"]:
        st.write(f"**Owner suggestion:** {item['owner_suggestion']}")

    st.divider()
    st.write("**Search for a facility:**")
    q = st.text_input("Facility name contains...", key=f"search_{item['track_id']}")
    if q:
        results = search_facilities(q)
        for f in results:
            label = f"{f['facility_name']} -- {f.get('city') or '?'}, {f.get('state_code') or '?'} (id {f['facility_id']})"
            if st.button(f"Link to: {label}", key=f"search_res_{item['track_id']}_{f['facility_id']}"):
                resolve(item["track_id"], "facility", facility_ids=[f["facility_id"]])
                st.rerun()

    st.divider()
    fac_raw = st.text_input("Facility ID(s), comma-separated for multi-course apps",
                            key=f"fac_{item['track_id']}")
    if st.button("Link to facility ID(s)", key=f"fac_btn_{item['track_id']}") and fac_raw.strip():
        try:
            fids = [int(x.strip()) for x in fac_raw.split(",") if x.strip()]
            resolve(item["track_id"], "facility", facility_ids=fids)
            st.rerun()
        except ValueError:
            st.error("Facility IDs must be numbers.")

    owner_raw = st.text_input("Owner ID", key=f"owner_{item['track_id']}")
    if st.button("Link to owner ID", key=f"owner_btn_{item['track_id']}") and owner_raw.strip():
        try:
            resolve(item["track_id"], "owner", owner_id=int(owner_raw.strip()))
            st.rerun()
        except ValueError:
            st.error("Owner ID must be a number.")

    st.divider()
    ex1, ex2 = st.columns(2)
    if ex1.button("Not a golf course", key=f"notgolf_{item['track_id']}"):
        resolve(item["track_id"], "exclude", exclude_reason="not_a_golf_course")
        st.rerun()

    country = ex2.text_input("Country", key=f"country_{item['track_id']}",
                             placeholder="canada")
    if ex2.button("Non-US", key=f"nonus_{item['track_id']}") and country.strip():
        resolve(item["track_id"], "exclude",
               exclude_reason=f"non_us: {country.strip().lower()}",
               country=country.strip().lower())
        st.rerun()
