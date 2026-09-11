# Golf Course App — Vendor Detection Pipeline

## Objective

For ~14,000 US golf facilities, determine (a) whether the facility has a native
mobile app, and (b) which software vendor built it (Sagacity, Gallus, Chronogolf,
foreUP, Teesnap, Club Prophet, etc.).

Output: a monthly-refreshed table of `facility → app → vendor`, plus a diff showing
new apps, vendor switches, and delisted apps.

---

## Critical context: why this is not a simple field lookup

These are **white-label apps**. One vendor ships the same codebase to hundreds of
courses with the branding swapped. The vendor's identity leaks into the App Store
listing inconsistently — there is **no single field that reliably names the vendor.**

Observed ground truth (verify each of these during calibration):

| App | Seller | Copyright | Dev website | Privacy policy | Vendor |
|---|---|---|---|---|---|
| Thorncreek Golf Tee Times (id 991127971) | Quick 18, Inc. | — | — | book.quick18.com | Sagacity |
| Sagacity 360 (id 1660735735) | Quick 18, Inc. | © Sagacity Golf Technologies | sagacitygolf.com | book.quick18.com | Sagacity |
| Cimarron Golf Resort | Cimarron Cathedral | Cimarron Cathedral, Inc. | sagacity.com | sagacity.com | Sagacity |
| Los Serranos Golf Tee Times (id 1358773907) | JC Resorts, LLC | © 2022 Gallus Golf | ? | ? | Gallus |
| Honey Brook Golf Club | Honey Brook Golf Club, LP | © 2022 Gallus Golf | ? | Gallus | Gallus |

Key implications:

1. **Every case is named by at least one field — never the same field twice.**
   Therefore: fetch the whole HTML page and regex ALL fields. Do not build around
   any single field.
2. **`copyright` and `privacyPolicyUrl` are NOT in the iTunes JSON.** They exist only
   on the `apps.apple.com` HTML page. Honey Brook is identifiable by nothing else.
3. **Publishing model varies by vendor.** Sagacity/Quick 18 keeps most apps in one
   developer account (artistId 433703118) → `artistId` expansion is powerful.
   Gallus publishes under *operator* accounts (2/2 observed) → artistId expansion is
   nearly useless; use the recommendation graph instead.
4. **Description boilerplate identifies a codebase lineage, not a vendor.** The
   phrase "share these reservations with your playing partners via text and email"
   appears in BOTH Sagacity-copyright apps (Thorncreek, Homestead, Coyote Lakes) AND
   a Gallus-copyright app (Los Serranos). Use it as a high-recall net to find
   candidates; use copyright to assign the vendor label. Never use the template
   alone for attribution.

---

## Verify before building (30 minutes, blocks design decisions)

Run these first and record the answers in `docs/findings.md`. Several assumptions
below are unconfirmed.

1. **Is `sellerUrl` in the iTunes lookup JSON?**
   `GET https://itunes.apple.com/lookup?id=1660735735&country=us`
   Check for `sellerUrl`, `privacyPolicyUrl`, `description`, `copyright`.
   → If `sellerUrl` is present, it becomes a cheap pre-filter. If absent, all
   website matching moves to the HTML pass. **Do not assume either way.**

2. **Does `/search` index app descriptions?**
   `GET https://itunes.apple.com/search?term=share+these+reservations+with+your+playing+partners&entity=software&country=us&limit=200`
   → If it returns the known template apps, the template sweep works via the API.
   If not, the template sweep must go through a web search API
   (Serper / Brave / SerpAPI) using `site:apps.apple.com "<phrase>"`.

3. **Does `limit=200` + `offset` actually paginate `/search`?**
   Apple's docs are thin here. Test `offset=200`. If pagination doesn't work,
   the geographic sweep needs many more, narrower queries.

4. **Confirm the batch lookup limit.**
   `GET https://itunes.apple.com/lookup?id=<100 comma-separated ids>&country=us`
   Assert `len(results) == len(requested_ids)` minus known-missing. Silent
   truncation is the expected failure mode.

5. **Actual rate limit.** Empirically find where 403s start. Assumption is
   ~20 req/min per IP; verify rather than trust.

---

## Architecture

Three phases. Phase 1 runs once (~4-5h wall clock). Phase 3 runs monthly (<1h).

```
Phase 1: SWEEP        build the golf-app corpus (~5-15k apps)
Phase 2: JOIN         match 14k facilities → corpus locally, then residual search
Phase 3: ENRICH       fetch HTML for candidates, regex all fields, assign vendor
```

**Do not do per-facility search first.** 14,000 facilities × 3 name variants =
42,000 queries of massively overlapping work. Build the corpus first (~1,200
queries), join locally, and only search the facilities that fail to match.

### Phase 1 — Sweep (~1,200 queries, ~75 min at 4 workers)

All via `GET https://itunes.apple.com/search?entity=software&country=us&limit=200`.

| Sweep | Count | Notes |
|---|---|---|
| Template phrases | ~50 | Boilerplate variants. Note "The app also support" (sic) AND "supports" — the typo was fixed at some point; search both. |
| Generic terms | ~50 | "golf tee times", "book tee times", "tee sheet", "golf course app", "golf gps scorecard", "golf club app" |
| Geographic | ~900 | 50 states + ~400 golf metros × 2 patterns: `"golf {place}"`, `"{place} tee times"` |
| Vendor names | ~50 | sagacity, quick 18, gallus, chronogolf, foreup, teesnap, club prophet, golfnow, lightspeed |
| artistId expansion | ~200 | Every distinct `artistId` found above → `lookup?id={artistId}&entity=software&limit=200` |

artistId expansion is the highest-yield step — one call returns an entire portfolio.
Run it iteratively: expand, find new artistIds in results, expand again, until fixpoint.

### Phase 2 — Join, then residual

1. Normalize facility names: strip `golf|club|course|resort|country|links|the|at|and|gc|cc`.
2. Parse app descriptions for course name + city (they state it explicitly:
   "provides tee time booking for X Golf Course in Y, ST").
3. Fuzzy match facility ↔ app on normalized name + city (rapidfuzz, threshold ~85).
4. **Residual queue**: only unmatched facilities get individual searches.
   Prioritize by app likelihood — resorts, multi-course groups, and higher-green-fee
   public courses first. Municipals and 9-holes last. Allow early stop when yield
   flattens.

### Phase 3 — Enrich

Fetch `https://apps.apple.com/us/app/id{trackId}` for every candidate app
(NOT every facility). ~2h at 4 workers × 1s delay for ~30k pages; far less if the
corpus is smaller.

Extract: `seller`, `copyright`, `developer website` href, `privacy policy` href,
`support url`, `artistId`, description.

Then regex **all fields at once** against the vendor token list. Record which
field(s) matched and a confidence tier.

---

## Vendor fingerprint config

Externalize to `vendors.yaml` so adding a vendor requires no code change:

```yaml
sagacity:
  display_name: Sagacity Golf
  tokens: [sagacity, "quick 18", quick18]
  bundle_prefixes: ["com.quick18."]
  known_artist_ids: [433703118]
  copyright_patterns: ["Sagacity Golf", "Quick 18"]
  domains: [sagacitygolf.com, sagacity.com, quick18.com, book.quick18.com]
  publishing_model: vendor_account   # artistId expansion is high-yield

gallus:
  display_name: Gallus Golf
  tokens: [gallus]
  copyright_patterns: ["Gallus Golf"]
  domains: [gallusgolf.com]
  publishing_model: operator_account # artistId expansion useless; use rec graph
```

Confidence tiers:

- **high** — copyright match, or bundleId prefix match, or vendor-account artistId
- **medium** — developer website or privacy policy domain match
- **low** — description template match only (lineage, not vendor — see note above)
- **unknown** — golf booking app, no vendor signal. **Track these explicitly.**
  This bucket is the honest measure of pipeline blind spots. Do not silently drop.

---

## Expansion for operator-published vendors

For vendors like Gallus with no shared account, use Apple's recommendation graph:

```
https://apps.apple.com/us/app/{id}?see-all=customers-also-bought-apps
```

Observed signal: Honey Brook (PA, Gallus) recommends Ron Jaworski Golf (NJ) —
cross-state, so it is clustering on something other than geography. Traverse this
graph from every confirmed vendor app, breadth-first, 2 hops max, feeding results
back into Phase 3. Cap total expansion to avoid runaway.

---

## Schema (SQLite)

```sql
facilities(facility_id PK, name, name_normalized, city, state, website, type, tier)
apps(track_id PK, bundle_id, track_name, seller_name, artist_id, description,
     seller_url, privacy_policy_url, copyright, primary_genre,
     release_date, current_version_release_date, first_seen, last_seen,
     html_fetched_at, delisted_at)
app_vendor(track_id, vendor, confidence, matched_fields JSON, detected_at)
facility_app(facility_id, track_id, match_confidence, match_method)  -- MANY-TO-MANY
search_cache(query_normalized PK, track_ids JSON, fetched_at)
facility_no_app(facility_id, last_searched_at, search_count)  -- negatives ARE data
snapshots(run_id, run_date, facility_id, track_id, vendor)     -- for diffing
```

Notes:
- `facility_app` **must** be many-to-many. Multi-course apps exist ("Oklahoma Golf
  Trail", "Cragun's Resort Golf Courses"). Retrofitting this later is painful.
- `facility_no_app` drives penetration metrics and lets you back off re-searching
  ~10k empty facilities monthly → quarterly.
- Never overwrite `snapshots`; the month-over-month diff is the product.

---

## Rate limiting and resilience

- Assume ~20 req/min per IP until measured. Throttle 3.5s between calls per worker.
- 4 workers on distinct egress IPs (Lambda / Cloud Run / small VPSs). Do not exceed
  ~8 — backoff losses exceed throughput gains.
- Exponential backoff on 403/429, max 4 retries.
- **Checkpoint to SQLite after every call.** `--resume` must be free. A 4-hour job
  will be interrupted.
- Normalize and cache every query string; never issue the same query twice, including
  across monthly runs.
- `country=us` only. Do not multiply by storefronts.
- Set a descriptive User-Agent. Datacenter IPs get throttled harder than residential;
  if 403s dominate, lengthen the delay before adding workers.

---

## CLI

```
golfapps verify                    # run the 5 pre-build checks, write docs/findings.md
golfapps calibrate                 # blind run vs ground-truth set, report recall
golfapps sweep [--resume]          # Phase 1
golfapps join                      # Phase 2 local match + residual queue
golfapps residual [--limit N]      # Phase 2 residual searches
golfapps enrich [--resume]         # Phase 3 HTML fetch + vendor labelling
golfapps report                    # penetration + vendor market share
golfapps diff --since YYYY-MM      # monthly changes
golfapps monthly                   # orchestrates the recurring run
```

---

## Calibration — build this before the full run

**Do not commit 5 hours of crawling before measuring recall.**

Ground truth set: scrape the published client lists from sagacitygolf.com and
gallusgolf.com (both publish course lists / case studies), plus these known apps:

- Sagacity/Quick 18: Thorncreek, Homestead, Coyote Lakes, Temecula Creek, The Pearl,
  St. Lucia Links, Canyons, Yarrambat, Meadowlark, Meadowlands, Cowboys, Cimarron,
  Redhawk, Teravista, Gold Canyon, Pearland, Goat Hill Park, Oklahoma Golf Trail,
  Quailwood Greens, DeLaveaga, Cragun's, Stone Creek, Newport, North Hampton, GCU,
  Legend Trail, Angel Park, Clover Hill, Swing First
- Gallus: Los Serranos, Honey Brook, Ron Jaworski Golf (verify)

`golfapps calibrate` runs the pipeline against ONLY these facility names, as if the
answers were unknown, and reports:

- recall per vendor (found / known)
- which detection mechanism caught each (copyright / domain / bundle / artistId / template / rec-graph)
- **which known apps were missed, and which field would have caught them**

That last line is the one that matters. It tells you where the blind spots are
before you spend the crawl budget, and it is the number that should decide whether
this approach ships.

Target: >90% recall per vendor. Below ~70%, stop and reconsider rather than scaling up.

---

## Monthly run (<1h)

1. Batch-lookup all known `track_id`s (100/call, ~350 calls, 20 min) — catches
   delisting, version changes, metadata edits.
2. Re-run template + vendor + generic queries (~150) — catches new entrants.
3. Residual search only for facilities due (changed, or quarterly rotation).
4. Enrich only apps whose `current_version_release_date` changed, or newly found.
5. Write snapshot, emit diff.

---

## Deliverables

1. Python package, SQLite state, resumable, `uv` or `pip -e` installable.
2. `docs/findings.md` — answers to the 5 verification questions, with raw evidence.
3. `vendors.yaml` — externalized fingerprints.
4. Calibration report with per-vendor recall and per-mechanism attribution.
5. `report` output: app penetration across 14k facilities + vendor market share
   + count of `unknown`-vendor golf apps (the blind-spot metric).

## Non-goals

- Download/revenue estimates.
- Non-US storefronts.
- Google Play (worth adding later — Play exposes developer name and email, and
  package names appear in indexable URLs — but scope v1 to iOS).
- Any paid app-intelligence vendor. This pipeline exists to avoid that cost.
