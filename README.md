# golfapps — facility → app → vendor detection

Determines, for the 13,922 US golf facilities in Supabase, whether each has a native
mobile app and which software vendor built it. Produces a monthly-refreshed table plus
a diff showing new apps, vendor switches, and delistings.

Every constant in `config.py` traces to a measurement in [docs/findings.md](docs/findings.md).
Read that first — it documents where the original plan's assumptions were wrong and why
the architecture differs from it.

## Install

```bash
cd C:\Downgrain\appdetector
pip install -e .
```

Credentials come from `C:\Downgrain\person\.env` (`SUPABASE_URL`, `SUPABASE_KEY` — must
be the service_role key, since `query_sql` is granted only to it).

## Commands

```
golfapps initdb                     # create the ga_* tables (idempotent)
golfapps vendors                    # show loaded fingerprints, no DB needed
golfapps sweep [--limit N] [--no-geo]   # Phase 1: build the app corpus
golfapps enrich [--no-escalate] [--max-html N]  # Phase 3: label vendors
golfapps join [--limit N]           # Phase 2: match apps to facilities
golfapps calibrate                  # recall + attribution vs known pairs
golfapps report [--json]            # penetration, market share, blind spots
golfapps refresh                    # re-lookup every known app (delisting check)
golfapps snapshot / diff --since run_202608
golfapps monthly                    # orchestrates the recurring run
```

Phase order is **sweep → enrich → join**: labelling before matching means the join only
considers apps worth matching.

## Why it is shaped this way

**State lives in Supabase, not SQLite.** Render cron jobs cannot mount persistent disks,
so a SQLite file would be wiped every run. The crawl is rate-limited to ~30 req/min
anyway, which makes a network write per call free in practice.

**The sweep splits truncated queries.** `/search` ignores `offset` entirely and caps near
190 results per query. Any query returning ≥185 is hiding results, so it is split
geographically (state → cities) until it clears the ceiling. Without this the sweep
silently loses the densest golf markets.

**There is no description-template sweep.** `/search` does not index description bodies
at all — the phrase queries the plan specified return couples' games, not golf apps.

**Batch lookups are pinned at 200 ids.** At 300 the API returns HTTP 200 with only ~210
rows — silent truncation. `itunes.lookup_batch` asserts its own response length; a large
shortfall raises rather than being mistaken for delisting.

**403 is not 429.** A 429 is transient and gets a short backoff. A 403 is a sticky penalty
box that short retries make worse, so the client stands down for ten minutes.

**HTML is an escalation, not a default pass.** `bundle_id` + `seller_url` + `seller_name`
label ~40–50% of course apps from the free JSON the sweep already collected. Only
`low`/`unknown` labels — plus any label resting *solely* on the stale-prone `seller_url`
or `developer_website` — get a page fetch.

## Vendor attribution

`vendors.yaml` holds all 15 vendors and needs no code change to extend. Fields are ranked
by authority because listings routinely disagree with themselves:

| authority | field | why |
|---|---|---|
| 100 | `seller_name`, `artist_name` | who publishes it today; resolves resellers |
| 90 | `copyright` | updated on migration |
| 80 | `privacy_policy_url`, `support_url` | track the live vendor |
| 70 | `bundle_id` | durable, but survives migration and resale |
| 50 | `seller_url`, `developer_website` | go stale |
| 10 | `description` | codebase lineage, never attribution |

Three collisions this ranking exists to handle, all observed live:

- **Teesnap resells Gallus.** 27 apps carry `com.gallusgolf.*` bundles with
  `sellerName = "Teesnap, LLC"`. Bundle-prefix matching alone books them as Gallus.
- **Los Serranos migrated Quick18 → Gallus.** Its `seller_url` still says quick18.com
  while copyright and privacy say Gallus. Resolved to Gallus, and the disagreement is
  recorded — a vendor switch is a product signal, not noise.
- **ClubHouse Online is a Jonas product** sharing `com.jonassoftware.*` entirely. A child
  vendor is promoted over its parent, since only the seller domain separates them.

`golfapps calibrate` reports two numbers, deliberately separated:

- **attribution accuracy** — of ground-truth apps found, how many got the right vendor.
  This measures the labeller.
- **recall** — found *and* labelled correctly. This also measures sweep coverage.

Vendors with no per-facility listing, and vendor *customer* lists (which do not assert an
app exists), are excluded from the headline and reported separately — averaging them in
would measure the wrong thing.

## Known limits

**Four vendors cannot be detected this way at all.** ForeTees, Buz Club, Whoosh and Club
Prophet ship a single multi-tenant app, so their client courses have no listing to find.
Their market share in `report` is a **floor, not an estimate**, and the report says so.
Closing that gap needs vendor client lists or Google Play, not more crawling.

**iOS only.** The schema carries a `store` column so Google Play can be added without a
migration.

**`ga_snapshot` is append-only.** The month-over-month diff is the product; never
overwrite it.

## Tests

```bash
python tests/test_vendors.py
```

14 labelling cases plus two behavioural guarantees (description alone never scores high
confidence; a two-vendor listing is flagged as a conflict). Every case is a real listing
observed during verification.
