"""Configuration. Every constant here traces to a measurement in docs/findings.md."""
import os
from pathlib import Path

from dotenv import load_dotenv

# A .env in this repo's own root takes priority -- self-contained, works on
# Render (which has no C:\Downgrain\person to read) and anywhere this repo
# gets cloned. load_dotenv() doesn't override already-set values, so loading
# this FIRST means it wins over the shared file below when both define the
# same key. Falls back to the shared Downgrain dotenv (same path
# facility_adder and delta_processor use) for any var not in the local one --
# harmless no-op if that path doesn't exist (e.g. on Render, or someone else's
# machine).
load_dotenv(Path(__file__).resolve().parent.parent / ".env")
ENV_PATH = Path(r"C:\Downgrain\person\.env")
load_dotenv(ENV_PATH)

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")  # must be service_role: query_sql is restricted to it

PKG_DIR = Path(__file__).parent
VENDORS_YAML = PKG_DIR / "vendors.yaml"
SCHEMA_SQL = PKG_DIR / "schema.sql"

# ---------------------------------------------------------------- facility scope
# 'Golf Course' + Active/Pending only. Closed is excluded -- confirmed
# deliberately, Derek 2026-08-20 (briefly reconsidered over the del Lago
# Golf Club case -- exact domain match, but genuinely closed -- then
# confirmed a closed course should not be matched at all, not just filtered
# downstream).
FACILITY_WHERE = (
    "facility_type = 'Golf Course' "
    "AND active_status IN ('Active', 'Pending Opening') "
    "AND country = 'USA'"
)

# ---------------------------------------------------------------- iTunes API
ITUNES_SEARCH = "https://itunes.apple.com/search"
ITUNES_LOOKUP = "https://itunes.apple.com/lookup"
APPSTORE_PAGE = "https://apps.apple.com/us/app/id{track_id}"
COUNTRY = "us"

# Check 5b: 30 rpm of NOVEL queries measured clean (0% 429); 60 rpm -> 5%; 90 -> 18%.
# 2.0s spacing = 30 rpm. Cached repeats are nearly free but we never repeat anyway.
API_MIN_INTERVAL_S = 2.0

# Check 5c: 429 is soft and transient. 403 is a sticky penalty box -- 14 consecutive
# 403s through a 5s..160s backoff. Retrying a 403 quickly extends the outage, so we
# stand down for a fixed long period instead.
RETRY_429_BACKOFF_S = [15, 30, 60, 120]
PENALTY_403_COOLDOWN_S = 600
MAX_RETRIES = 4

# Check 3: /search returns at most ~187-190 results regardless of `limit`, and `offset`
# is ignored entirely. A query at or above this is truncated and MUST be split.
SEARCH_LIMIT = 200
SEARCH_CEILING = 185

# Check 4: 200 ids/call returns 200 rows clean. 300 returns 210 with HTTP 200 --
# silent truncation. 500+ returns 502. Never raise this.
LOOKUP_BATCH_SIZE = 200

# An id absent on the first lookup pass gets one retry this many seconds later
# before refresh_known_apps() finalizes it as delisted (Derek, 2026-09-11: a
# monthly cadence means one bad Apple API response shouldn't be enough to mark a
# real, live app dead for a whole month).
DELIST_RETRY_DELAY_S = 300

# Check 5d: apps.apple.com is Fastly-cached, measured 316 req/min sequential with
# zero blocks over 60 requests. Different infrastructure from the iTunes API.
HTML_MIN_INTERVAL_S = 0.3

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36 golfapps/0.1 (+derek@downgrain.com)"
)

# ---------------------------------------------------------------- match service
MATCH_API_BASE = "https://match-service-2-0.onrender.com"
MATCH_CONFIDENCE_THRESHOLD = 0.85
MATCH_POLL_INTERVAL_S = 5
MATCH_BATCH_SIZE = 500

# ---------------------------------------------------------------- misc
STORE = "ios"  # schema is store-agnostic so Google Play can be added without migration


def require_credentials() -> None:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError(
            f"SUPABASE_URL / SUPABASE_KEY not found in {ENV_PATH}. "
            "query_sql requires the service_role key."
        )


# ---------------------------------------------------------------- proxy (Evomi)
# A degraded/penalized direct IP blocks even normally-open endpoints like
# apps.apple.com HTML (measured 316 rpm / 0 blocks directly, then blocked
# outright the same night after heavy direct-IP search-API traffic -- see
# appstore.AppStoreClient's proxy note, 2026-08-23). Optional: every caller
# that accepts a `proxies` dict falls back to a direct connection if this
# returns None, so nothing breaks when Evomi isn't configured.
EVOMI_HOST = os.getenv("EVOMI_HOST")
EVOMI_PORT = os.getenv("EVOMI_PORT")
EVOMI_USERNAME = os.getenv("EVOMI_USERNAME")
EVOMI_PASSWORD = os.getenv("EVOMI_PASSWORD")


def proxy_config() -> dict | None:
    """{'http': ..., 'https': ...} for requests, or None if Evomi isn't configured."""
    if not (EVOMI_HOST and EVOMI_PORT and EVOMI_USERNAME and EVOMI_PASSWORD):
        return None
    url = f"http://{EVOMI_USERNAME}:{EVOMI_PASSWORD}@{EVOMI_HOST}:{EVOMI_PORT}"
    return {"http": url, "https": url}
