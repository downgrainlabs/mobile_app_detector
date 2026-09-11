"""Extract every vendor-bearing field from apps.apple.com HTML and compare
against the iTunes JSON, for the plan's ground-truth apps."""
import html as htmllib
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
    if tag:
        with open(os.path.join(RAW, f"{tag}.{ext}"), "w", encoding="utf-8") as f:
            f.write(r.text)
    print(f"  [{tag or url[:60]}] {r.status_code} {len(r.content)}B")
    return r


def jget(url, tag=None):
    r = get(url, tag)
    try:
        return r.json()
    except Exception:
        return json.loads(r.text.strip())


def clean(s):
    return htmllib.unescape(re.sub(r"<!--.*?-->", "", s or "")).strip()


def extract_html(h):
    """Pull every vendor-bearing field out of an App Store product page."""
    out = {}
    # Copyright: rendered as <dt>Copyright</dt><dd><ul><li>...</li>
    m = re.search(r"<dt[^>]*>Copyright</dt>\s*<dd><ul><li[^>]*>(.*?)</li>", h, re.S)
    out["copyright"] = clean(m.group(1)) if m else None
    # Seller: <dt>Seller</dt>
    m = re.search(r"<dt[^>]*>Seller</dt>\s*<dd><ul><li[^>]*>(.*?)</li>", h, re.S)
    out["seller"] = clean(m.group(1)) if m else None
    # Labeled external links (Developer Website / Privacy Policy / Support)
    links = {}
    for m in re.finditer(
        r'<a[^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', h, re.S
    ):
        label = re.sub(r"<[^>]+>", " ", m.group(2))
        label = re.sub(r"\s+", " ", htmllib.unescape(label)).strip()
        if label and len(label) < 60:
            links.setdefault(label, m.group(1))
    out["developer_website"] = links.get("Developer Website")
    out["app_support"] = links.get("App Support")
    out["privacy_policy"] = links.get("Privacy Policy")
    # aria-labelled privacy policy inside the privacy card
    m = re.search(r'aria-label="Developer.{0,3}s Privacy Policy"[^>]*href="([^"]+)"', h)
    out["privacy_policy_aria"] = m.group(1) if m else None
    # from the serialized-server-data blob
    out["ssd_supportUrl"] = list(dict.fromkeys(
        re.findall(r'"url":"(https?://[^"]*support[^"]*)"', h)))[:4]
    # every distinct external host on the page
    hosts = set()
    for u in re.findall(r'https?://([a-z0-9.\-]+)', h, re.I):
        hosts.add(u.lower())
    out["nonapple_hosts"] = sorted(x for x in hosts
                                   if not x.endswith(("apple.com", "mzstatic.com",
                                                      "cdn-apple.com", "apple.co")))[:25]
    return out


VENDOR_TOKENS = {
    "sagacity": ["sagacity", "quick 18", "quick18"],
    "gallus": ["gallus"],
}


def label(fields, itunes):
    hits = {}
    hay = {
        "copyright": fields.get("copyright") or "",
        "seller": fields.get("seller") or "",
        "developer_website": fields.get("developer_website") or "",
        "privacy_policy": (fields.get("privacy_policy") or "") + " " + (fields.get("privacy_policy_aria") or ""),
        "app_support": fields.get("app_support") or "",
        "itunes_sellerUrl": itunes.get("sellerUrl") or "",
        "bundleId": itunes.get("bundleId") or "",
        "description": itunes.get("description") or "",
    }
    for vendor, toks in VENDOR_TOKENS.items():
        for field, text in hay.items():
            t = text.lower()
            for tok in toks:
                if tok in t:
                    hits.setdefault(vendor, []).append(field)
                    break
    return {k: sorted(set(v)) for k, v in hits.items()}


GROUND = [
    (991127971, "Thorncreek Golf Tee Times", "Sagacity"),
    (1660735735, "Sagacity 360", "Sagacity"),
    (1358773907, "Los Serranos Golf Tee Times", "Gallus"),
]

# Also resolve a few plan-named apps not in the Quick 18 account, to test the
# "most Sagacity apps live under artistId 433703118" claim.
DISPERSION_QUERIES = [
    "Cimarron Golf Resort", "Coyote Lakes Golf", "Angel Park Golf",
    "Honey Brook Golf", "Ron Jaworski Golf", "DeLaveaga Golf",
    "Goat Hill Park", "Oklahoma Golf Trail", "Cragun's Resort Golf",
    "Teravista Golf", "Redhawk Golf Temecula", "Gold Canyon Golf",
]

report = {}

print("=== GROUND TRUTH: HTML vs iTunes JSON ===")
gt = {}
for tid, name, claimed in GROUND:
    it = jget(f"https://itunes.apple.com/lookup?id={tid}&country=us")
    it = (it.get("results") or [{}])[0]
    r = get(f"https://apps.apple.com/us/app/id{tid}", f"x_html_{tid}", ext="html")
    f = extract_html(r.text)
    gt[name] = {
        "trackId": tid,
        "plan_says_vendor": claimed,
        "HTML": {k: v for k, v in f.items() if k != "nonapple_hosts"},
        "HTML_nonapple_hosts": f["nonapple_hosts"],
        "ITUNES": {
            "sellerName": it.get("sellerName"), "sellerUrl": it.get("sellerUrl"),
            "artistId": it.get("artistId"), "artistName": it.get("artistName"),
            "bundleId": it.get("bundleId"),
            "currentVersionReleaseDate": it.get("currentVersionReleaseDate"),
            "releaseDate": it.get("releaseDate"),
            "description_head": (it.get("description") or "")[:200],
        },
        "TOKEN_HITS": label(f, it),
    }
report["ground_truth"] = gt

print("\n=== DISPERSION: which artistId publishes each plan-named app? ===")
disp = {}
for q in DISPERSION_QUERIES:
    d = jget("https://itunes.apple.com/search?term=" + urllib.parse.quote_plus(q)
             + "&entity=software&country=us&limit=5")
    rows = d.get("results", [])
    disp[q] = [{"trackId": x.get("trackId"), "trackName": x.get("trackName"),
                "artistId": x.get("artistId"), "artistName": x.get("artistName"),
                "bundleId": x.get("bundleId"), "sellerUrl": x.get("sellerUrl")}
               for x in rows[:3]]
report["dispersion"] = disp

p = os.path.join(RAW, "extract_summary.json")
with open(p, "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2, ensure_ascii=False)
print("\nwrote", p)
print(json.dumps(report, indent=2, ensure_ascii=False)[:12000])
