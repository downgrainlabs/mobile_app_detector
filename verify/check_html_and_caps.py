"""Follow-up probes: HTML field availability, /search result ceiling,
artistId expansion ceiling, and the real batch-lookup limit."""
import json
import os
import re
import time
import urllib.parse

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "..", "docs", "raw")
os.makedirs(RAW, exist_ok=True)

S = requests.Session()
S.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36 "
                  "golfapps-research/0.1 (+derek@downgrain.com)",
    "Accept-Language": "en-US,en;q=0.9",
})
DELAY = 3.5
_last = [0.0]


def get(url, tag, ext="json"):
    wait = DELAY - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    r = S.get(url, timeout=40)
    _last[0] = time.time()
    with open(os.path.join(RAW, f"{tag}.{ext}"), "w", encoding="utf-8") as f:
        f.write(r.text)
    print(f"[{tag}] {r.status_code} {len(r.content)}B  {url[:100]}")
    return r


out = {}

# ---------------------------------------------------------- A: HTML fields
print("\n=== A: apps.apple.com HTML — do copyright / privacy policy exist there? ===")
GROUND = [
    (991127971, "thorncreek", "Sagacity per plan"),
    (1660735735, "sagacity360", "Sagacity per plan"),
    (1358773907, "los_serranos", "Gallus per plan"),
]
a = {}
for tid, name, claim in GROUND:
    r = get(f"https://apps.apple.com/us/app/id{tid}", f"html_{name}_{tid}", ext="html")
    h = r.text
    rec = {"http": r.status_code, "bytes": len(h), "plan_claim": claim}
    if r.status_code != 200:
        a[name] = rec
        continue
    # Apple embeds a JSON blob in <script id="shoebox-media-api-cache-apps" ...>
    blob = None
    m = re.search(r'<script[^>]*id="shoebox-media-api-cache-apps"[^>]*>(.*?)</script>', h, re.S)
    if m:
        try:
            outer = json.loads(m.group(1))
            for v in outer.values():
                inner = json.loads(v)
                d = inner.get("d") or []
                for item in d:
                    if str(item.get("id")) == str(tid):
                        blob = item
            rec["shoebox_found"] = True
        except Exception as e:
            rec["shoebox_parse_error"] = str(e)
    else:
        rec["shoebox_found"] = False

    if blob:
        attrs = blob.get("attributes", {})
        rec["shoebox_attribute_keys"] = sorted(attrs.keys())
        rec["copyright"] = attrs.get("copyright")
        rec["name"] = attrs.get("name")
        rec["sellerName"] = attrs.get("artistName") or attrs.get("sellerName")
        rec["privacyPolicyUrl"] = attrs.get("privacyPolicyUrl")
        offers = attrs.get("offers")
        rec["platformAttrs_keys"] = sorted((attrs.get("platformAttributes") or {}).get("ios", {}).keys()) if attrs.get("platformAttributes") else None
        ios = ((attrs.get("platformAttributes") or {}).get("ios") or {})
        rec["ios_sellerUrl"] = ios.get("sellerUrl")
        rec["ios_privacyPolicyUrl"] = ios.get("privacyPolicyUrl")
        rec["ios_supportUrl"] = ios.get("supportUrl")
        rec["ios_bundleId"] = ios.get("bundleId")
        rec["_offers_present"] = bool(offers)
        with open(os.path.join(RAW, f"shoebox_{name}_{tid}.json"), "w", encoding="utf-8") as f:
            json.dump(blob, f, indent=2, ensure_ascii=False)

    # regex fallback straight off raw HTML
    rec["regex_copyright_hits"] = list(dict.fromkeys(
        re.findall(r'["\'>]\s*(©[^"<\']{0,80})', h)))[:6]
    rec["regex_copyright_field"] = re.findall(r'"copyright"\s*:\s*"([^"]{0,120})"', h)[:3]
    rec["regex_privacy_hrefs"] = list(dict.fromkeys(
        re.findall(r'"privacyPolicyUrl"\s*:\s*"([^"]+)"', h)))[:5]
    rec["regex_seller_url"] = list(dict.fromkeys(
        re.findall(r'"sellerUrl"\s*:\s*"([^"]+)"', h)))[:5]
    rec["regex_supportUrl"] = list(dict.fromkeys(
        re.findall(r'"supportUrl"\s*:\s*"([^"]+)"', h)))[:5]
    rec["mentions_gallus"] = len(re.findall(r'(?i)gallus', h))
    rec["mentions_sagacity"] = len(re.findall(r'(?i)sagacity', h))
    rec["mentions_quick18"] = len(re.findall(r'(?i)quick\s*18|quick18', h))
    a[name] = rec
out["A_html_fields"] = a

# ---------------------------------------------- B: is there a /search ceiling?
print("\n=== B: /search result ceiling ===")
b = {}
for term, lim in [("golf", 10), ("golf", 50), ("golf", 100), ("golf", 200),
                  ("game", 200), ("golf tee times", 200), ("tee times", 200)]:
    q = urllib.parse.quote_plus(term)
    r = get(f"https://itunes.apple.com/search?term={q}&entity=software&country=us&limit={lim}",
            f"cap_{q}_{lim}")
    try:
        d = r.json()
    except Exception:
        d = json.loads(r.text.strip())
    b[f"{term} | limit={lim}"] = {"resultCount": d.get("resultCount"),
                                  "n": len(d.get("results", []))}
out["B_search_ceiling"] = b

# ------------------------------- C: does artistId expansion return everything?
print("\n=== C: artistId expansion ceiling (Quick 18 = 433703118) ===")
c = {}
r = get("https://itunes.apple.com/lookup?id=433703118&entity=software&limit=200&country=us",
        "artist_433703118_limit200")
d = r.json() if r.headers.get("content-type", "").startswith("text/j") or True else None
try:
    d = r.json()
except Exception:
    d = json.loads(r.text.strip())
apps_api = [x for x in d.get("results", []) if x.get("wrapperType") == "software"]
c["lookup_entity_software_limit200"] = {
    "n": len(apps_api),
    "names": sorted(x.get("trackName") for x in apps_api),
}
# Cross-check against the developer page HTML
r = get("https://apps.apple.com/us/developer/quick-18-inc/id433703118",
        "developer_quick18_433703118", ext="html")
h = r.text
ids_html = sorted(set(re.findall(r'/app/[^"\']*?/id(\d{6,12})', h)))
c["developer_html"] = {"http": r.status_code, "bytes": len(h),
                       "distinct_app_ids_in_html": len(ids_html),
                       "ids_not_in_api": [i for i in ids_html
                                          if int(i) not in {x["trackId"] for x in apps_api}][:60]}
out["C_artist_expansion"] = c

# ------------------------------------------- D: real batch lookup limit
print("\n=== D: batch lookup limit ===")
q = urllib.parse.quote_plus("golf")
r = get(f"https://itunes.apple.com/search?term={q}&entity=software&country=us&limit=200",
        "pool_golf")
try:
    d = r.json()
except Exception:
    d = json.loads(r.text.strip())
pool = [x["trackId"] for x in d.get("results", []) if x.get("trackId")]
q2 = urllib.parse.quote_plus("tee times")
r = get(f"https://itunes.apple.com/search?term={q2}&entity=software&country=us&limit=200",
        "pool_teetimes")
try:
    d2 = r.json()
except Exception:
    d2 = json.loads(r.text.strip())
pool += [x["trackId"] for x in d2.get("results", []) if x.get("trackId") and x["trackId"] not in pool]
print(f"    pool size = {len(pool)}")

dd = {"pool_size": len(pool)}
for n in (50, 100, 150, 200, 300):
    if len(pool) < n:
        dd[f"batch_{n}"] = {"skipped": f"pool={len(pool)}"}
        continue
    ids = pool[:n]
    url = "https://itunes.apple.com/lookup?id=" + ",".join(map(str, ids)) + "&country=us"
    r = get(url, f"batch_{n}")
    try:
        dj = r.json()
        got = [x.get("trackId") for x in dj.get("results", [])
               if x.get("wrapperType") == "software"]
        dd[f"batch_{n}"] = {
            "http": r.status_code, "requested": n, "url_len": len(url),
            "resultCount": dj.get("resultCount"), "returned": len(got),
            "SILENT_TRUNCATION": len(got) < n,
            "n_missing": n - len(got),
            "missing_head": [i for i in ids if i not in set(got)][:10],
        }
    except Exception as e:
        dd[f"batch_{n}"] = {"http": r.status_code, "error": str(e),
                            "url_len": len(url), "body_head": r.text[:200]}
out["D_batch_limit"] = dd

p = os.path.join(RAW, "html_and_caps_summary.json")
with open(p, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print("\nwrote", p)
print(json.dumps(out, indent=2, ensure_ascii=False)[:9000])
