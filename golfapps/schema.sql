-- golfapps schema. Prefix ga_ so it is obvious which of the 231 tables belong here.
-- Store-agnostic: `store` column lets Google Play slot in without a migration.

-- ---------------------------------------------------------------- corpus
create table if not exists ga_app (
    track_id                    bigint primary key,
    store                       text        not null default 'ios',
    bundle_id                   text,
    track_name                  text,
    seller_name                 text,
    artist_id                   bigint,
    artist_name                 text,
    seller_url                  text,
    artwork_url                 text,
    description                 text,
    primary_genre               text,
    release_date                timestamptz,
    current_version_release_date timestamptz,
    -- HTML-only fields (Check 1: absent from the iTunes JSON)
    copyright                   text,
    privacy_policy_url          text,
    support_url                 text,
    developer_website           text,
    html_fetched_at             timestamptz,
    first_seen                  timestamptz not null default now(),
    last_seen                   timestamptz not null default now(),
    delisted_at                 timestamptz
);
create index if not exists ga_app_bundle_idx on ga_app (bundle_id);
create index if not exists ga_app_artist_idx on ga_app (artist_id);
create index if not exists ga_app_delisted_idx on ga_app (delisted_at);

-- ---------------------------------------------------------------- vendor labels
create table if not exists ga_app_vendor (
    track_id        bigint primary key references ga_app (track_id) on delete cascade,
    vendor          text,               -- null => golf booking app with no vendor signal
    confidence      text        not null,   -- high | medium | low | unknown
    matched_fields  jsonb       not null default '[]'::jsonb,
    -- Probe D: fields inside one listing can name DIFFERENT vendors (Los Serranos).
    -- That is a vendor switch in progress, and it is a product signal, not noise.
    conflict        boolean     not null default false,
    conflict_detail jsonb,
    detected_at     timestamptz not null default now()
);
create index if not exists ga_app_vendor_vendor_idx on ga_app_vendor (vendor);
create index if not exists ga_app_vendor_conflict_idx on ga_app_vendor (conflict) where conflict;

-- ---------------------------------------------------------------- facility links
-- MUST be many-to-many: multi-course apps exist (Oklahoma Golf Trail, Cragun's).
create table if not exists ga_facility_app (
    facility_id     bigint      not null,
    track_id        bigint      not null references ga_app (track_id) on delete cascade,
    match_confidence numeric,
    match_method    text,       -- match_service | bundle_hint | manual
    match_status    text,
    created_at      timestamptz not null default now(),
    primary key (facility_id, track_id)
);

-- Negatives are data: they drive penetration metrics and let the monthly run back
-- off from re-searching ~10k empty facilities.
create table if not exists ga_facility_no_app (
    facility_id     bigint primary key,
    last_searched_at timestamptz not null default now(),
    search_count    int         not null default 1
);

-- ---------------------------------------------------------------- crawl state
-- Check 3 + rate limits: never issue the same query twice, including across months.
create table if not exists ga_search_cache (
    query_normalized text primary key,
    track_ids        jsonb       not null default '[]'::jsonb,
    result_count     int,
    truncated        boolean     not null default false,  -- hit the ~190 ceiling
    fetched_at       timestamptz not null default now()
);

-- Queries that returned >= SEARCH_CEILING and were split into narrower children.
create table if not exists ga_query_split (
    parent_query    text        not null,
    child_query     text        not null,
    created_at      timestamptz not null default now(),
    primary key (parent_query, child_query)
);

-- ---------------------------------------------------------------- snapshots
-- Never overwrite. The month-over-month diff is the product.
create table if not exists ga_snapshot (
    run_id      text        not null,
    run_date    date        not null,
    facility_id bigint      not null,
    track_id    bigint,
    vendor      text,
    confidence  text,
    primary key (run_id, facility_id, track_id)
);
create index if not exists ga_snapshot_run_idx on ga_snapshot (run_id);

create table if not exists ga_run (
    run_id      text primary key,
    started_at  timestamptz not null default now(),
    finished_at timestamptz,
    phase       text,
    stats       jsonb
);

-- Ambiguous matches: the service found several plausible facilities. Recording the
-- candidates is what makes a later human decision cheap -- discarding them means
-- re-running the match to review anything. This is a work-product of the join, not
-- a decision store.
create table if not exists ga_match_ambiguous (
    track_id        bigint primary key references ga_app (track_id) on delete cascade,
    submitted_name  text,
    submitted_city  text,
    submitted_state text,
    match_status    text,
    candidates      jsonb not null default '[]'::jsonb,
    candidate_count int,
    seen_at         timestamptz not null default now()
);

-- Cloud Vision WEB_DETECTION results, cached per app.
-- The API costs money and the icon never changes, so a result is fetched at most
-- once. Raw response is kept so parsing can be revised without re-spending calls.
create table if not exists ga_web_detection (
    track_id            bigint primary key references ga_app (track_id) on delete cascade,
    image_variant       text,          -- cropped | full
    hosts               jsonb not null default '[]'::jsonb,   -- ranked candidate domains
    web_entities        jsonb not null default '[]'::jsonb,
    resolved_facility_id bigint,
    resolved_host       text,
    raw                 jsonb,
    fetched_at          timestamptz not null default now()
);

-- Human decisions about apps that automatic matching could not resolve.
-- Negative decisions are first-class: without them every monthly run re-presents
-- the same consumer apps and non-US courses forever.
create table if not exists ga_app_disposition (
    track_id     bigint primary key references ga_app (track_id) on delete cascade,
    disposition  text        not null,   -- facility | owner | not_a_facility_app
                                         -- | out_of_scope_geo | no_facility_in_db | unsure
    owner_id     bigint,                 -- when the app belongs to a group/municipality
    decided_by   text,
    decided_at   timestamptz not null default now(),
    note         text
);
create index if not exists ga_app_disposition_kind_idx on ga_app_disposition (disposition);

-- ---------------------------------------------------------------- MIGRATIONS
-- `create table if not exists` is a no-op on an existing table, so new columns on
-- already-deployed tables must be added explicitly. Keep these idempotent and
-- append-only; initdb runs the whole file every time.
alter table ga_app add column if not exists artwork_url text;

-- SerpAPI Google Lens results, cached per app. Free tier is 250 searches/month --
-- every result is fetched at most once, raw response kept for offline re-parsing.
create table if not exists ga_lens_detection (
    track_id             bigint primary key references ga_app (track_id) on delete cascade,
    hosts                jsonb not null default '[]'::jsonb,
    extracted_city       text,
    extracted_state      text,
    resolved_facility_id bigint,
    resolved_host        text,
    raw                  jsonb,
    fetched_at           timestamptz not null default now()
);
-- query_used: the app name sent alongside the icon via SerpAPI's `q` param. A row
-- with this NULL came from an image-only call and must be treated as stale once we
-- start querying with text -- the results are not comparable.
alter table ga_lens_detection add column if not exists query_used text;
-- Resolution now goes through match-service-2-0 (join.py's engine), not raw domain
-- string matching -- that is what the calibration run showed was unreliable
-- (fairmontmontana.com claimed by 4 unrelated apps). A link is only created if it
-- survives the SAME matcher Phase 2 already trusts.
alter table ga_lens_detection add column if not exists matcher_facility_id bigint;
alter table ga_lens_detection add column if not exists matcher_status text;
alter table ga_lens_detection add column if not exists matcher_confidence numeric;
-- Country the walk locked onto (US | CA | non-US | null=undetermined), plus the
-- province when it's Canada. Recorded even when it blocks a match today, because a
-- non-US course is a real, permanent answer -- not a gap -- and this data is what
-- lets it match for free the moment non-US facilities exist (Derek, 2026-08-18).
alter table ga_lens_detection add column if not exists detected_country text;
alter table ga_lens_detection add column if not exists detected_province text;

-- App Store subtitle (subheader) text -- subtitle_resolve.py's location signal.
alter table ga_app add column if not exists subtitle text;

-- ---------------------------------------------------------------- crosswalk
-- Durable, human-curated override -- the sole source of truth once a track_id has
-- a row here. Checked before match-service ever runs (join.apply_crosswalk()) and
-- never re-litigated by an automated re-run. Reconciled into this file 2026-09-11
-- -- the table has existed live since 2026-08-20, added ad hoc and never captured
-- here; `create table if not exists` is a no-op against that live table and only
-- matters for a from-scratch deploy.
create table if not exists app_crosswalk (
    track_id       bigint primary key references ga_app (track_id) on delete cascade,
    link_type      text        not null,   -- facility | owner | exclude
    facility_ids   bigint[],               -- link_type = 'facility': one or more ids
    owner_id       bigint,                 -- link_type = 'owner'
    exclude_reason text,                   -- link_type = 'exclude'
    country        text,
    source         text        not null,
    notes          text,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);
create index if not exists app_crosswalk_link_type_idx on app_crosswalk (link_type);

-- Same reconciliation, ga_app columns added ad hoc via scripts/apply_not_a_golf_course.py
-- and scripts/canada_domain_sweep.py, never captured here until now.
alter table ga_app add column if not exists not_a_golf_course_at timestamptz;
alter table ga_app add column if not exists canadian_course_at timestamptz;
alter table ga_app add column if not exists canadian_course_match text;

-- match_method on a snapshot row distinguishes an auto-matched link (domain, OCR,
-- subtitle, match_service) from one that came in through app_crosswalk
-- (manual_crosswalk -- includes Retool-resolved review items) -- outcome #4 vs a
-- resolved review-queue item, in report.diff().
alter table ga_snapshot add column if not exists match_method text;

-- ---------------------------------------------------------------- review queue
-- What rebuild.run() could not resolve on its own (vendor-confirmed, live, not
-- already in app_crosswalk, no facility/owner/name match found), populated fresh
-- every run by review_queue.sync(). Retool is the review surface; a decision there
-- calls resolve_review_item() below, which is the only way a row here and a row in
-- app_crosswalk can be written together -- keeps the two from ever disagreeing.
create table if not exists ga_review_queue (
    track_id             bigint primary key references ga_app (track_id) on delete cascade,
    app_name             text,
    vendor               text,
    confidence           text,
    bundle_id            text,
    seller_name          text,
    subtitle             text,
    app_store_url        text,
    description          text,
    candidates           jsonb       not null default '[]'::jsonb,
    owner_suggestion     text,
    status               text        not null default 'pending',  -- pending | resolved | delisted
    first_flagged_run_id text        not null,
    last_seen_run_id     text        not null,
    resolved_at          timestamptz,
    resolved_by          text,
    resolution           jsonb
);
create index if not exists ga_review_queue_status_idx on ga_review_queue (status);

-- Atomic write-back for a Retool decision: one call both writes the durable
-- app_crosswalk row AND flips this queue row, so the two tables can never
-- disagree about whether an app has been reviewed. p_decision is one of
-- 'facility' | 'owner' | 'exclude', matching app_crosswalk.link_type.
create or replace function resolve_review_item(
    p_track_id bigint,
    p_decision text,
    p_facility_ids bigint[] default null,
    p_owner_id bigint default null,
    p_exclude_reason text default null,
    p_country text default null,
    p_reviewer text default 'retool'
) returns void as $$
begin
    if p_decision not in ('facility', 'owner', 'exclude') then
        raise exception 'p_decision must be facility, owner, or exclude, got %', p_decision;
    end if;

    insert into app_crosswalk (track_id, link_type, facility_ids, owner_id,
                                exclude_reason, country, source, updated_at)
    values (p_track_id, p_decision, p_facility_ids, p_owner_id,
            p_exclude_reason, p_country, 'retool_review:' || p_reviewer, now())
    on conflict (track_id) do update
        set link_type = excluded.link_type,
            facility_ids = excluded.facility_ids,
            owner_id = excluded.owner_id,
            exclude_reason = excluded.exclude_reason,
            country = excluded.country,
            source = excluded.source,
            updated_at = now();

    update ga_review_queue
        set status = 'resolved',
            resolved_at = now(),
            resolved_by = p_reviewer,
            resolution = jsonb_build_object(
                'decision', p_decision, 'facility_ids', p_facility_ids,
                'owner_id', p_owner_id, 'exclude_reason', p_exclude_reason,
                'country', p_country)
        where track_id = p_track_id;
end;
$$ language plpgsql;
