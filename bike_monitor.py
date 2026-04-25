#!/usr/bin/env python3
"""
Bike Deal Finder v2
Monitors 9 sources for mountain bike deals matching specific criteria.

Scraping strategy by source:
  - The Pro's Closet, Jenson USA  → Shopify JSON API (pure HTTP, no browser)
  - Trek, Giant, Specialized, Marin → Playwright + network request interception
  - REI                            → Playwright + JS DOM extraction
  - Pinkbike Deals                 → Playwright + HTML scraping
  - Pinkbike Buy/Sell              → Playwright + HTML scraping (near 48823)

Uses Claude Haiku to intelligently score each listing against fit + value criteria.
"""

import json
import os
import re
import smtplib
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import anthropic
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from playwright_stealth import Stealth

_stealth = Stealth()

# ─────────────────────────────────────────────────────────────────────────────
#  Load .env for local runs
# ─────────────────────────────────────────────────────────────────────────────
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

EMAIL_ADDRESS  = "mattkaz@icloud.com"
EMAIL_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD", "")
NOTIFY_EMAILS  = ["mattkaz@icloud.com"]
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

# Location for Pinkbike buy/sell proximity context
BUYER_ZIP      = "48823"    # East Lansing, MI
BUYER_LOCATION = "East Lansing, MI"
MAX_TRAVEL_MILES = 100

STATE_FILE = Path(__file__).parent / "bike_state.json"
LOG_FILE   = Path(__file__).parent / "bike_monitor.log"

MIN_SCORE_TO_ALERT = 7   # Claude score out of 10

# ─────────────────────────────────────────────────────────────────────────────
#  SOURCES
# ─────────────────────────────────────────────────────────────────────────────

# Sources split by scraping strategy
#
# Shopify stores expose a public JSON endpoint at:
#   /collections/{collection-slug}/products.json
# No auth required. collection_slugs is a list of candidates to try in order.
SHOPIFY_SOURCES = [
    {
        "name":             "The Pro's Closet",
        "shop_url":         "https://www.theproscloset.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes", "bikes"],
    },
    {
        "name":             "Jenson USA",
        "shop_url":         "https://www.jensonusa.com",
        "collection_slugs": ["hardtail-cross-country", "hardtail-mountain-bikes", "mountain-bikes"],
    },
    {
        "name":             "Worldwide Cyclery",
        "shop_url":         "https://www.worldwidecyclery.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes-hardtail", "mountain-bikes"],
    },
    {
        "name":             "Fanatik Bike",
        "shop_url":         "https://www.fanatikbike.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes", "bikes"],
    },
    {
        "name":             "Cambria Bike",
        "shop_url":         "https://www.cambriabike.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes-hardtail", "mountain-bikes"],
    },
    {
        "name":             "Bicycle Warehouse",
        "shop_url":         "https://bicyclewarehouse.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes", "bikes"],
    },
    {
        "name":             "Mike's Bikes",
        "shop_url":         "https://www.mikesbikes.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes", "bikes"],
    },
    {
        "name":             "ERIK'S Bike Shop",
        "shop_url":         "https://www.eriksbikeshop.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes", "bikes"],
    },
    {
        "name":             "Universal Cycles",
        "shop_url":         "https://www.universalcycles.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes", "bikes"],
    },
    {
        "name":             "Velomine",
        "shop_url":         "https://www.velomine.com",
        "collection_slugs": ["mountain-bikes", "hardtail", "bikes"],
    },
    {
        "name":             "Bicycle Blue Book",
        "shop_url":         "https://www.bicyclebluebook.com",
        "collection_slugs": ["mountain-bikes", "hardtail-mountain-bikes", "bikes"],
    },
    {
        "name":             "Competitive Cyclist",
        "shop_url":         "https://www.competitivecyclist.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes", "bikes"],
    },
    {
        "name":             "Backcountry Bikes",
        "shop_url":         "https://www.backcountry.com",
        "collection_slugs": ["mountain-bikes-hardtail", "hardtail-mountain-bikes", "mountain-bikes"],
    },
]

PLAYWRIGHT_SOURCES = [
    {
        "name": "Trek",
        "url":  "https://www.trekbikes.com/us/en_US/bikes/mountain-bikes/hardtail-mountain-bikes/",
        "type": "intercept",
        "base_url": "https://www.trekbikes.com",
        "keywords": ["marlin", "roscoe", "hardtail"],
    },
    {
        "name": "Giant",
        "url":  "https://www.giant-bicycles.com/us/bikes/mountain/hardtail",
        "type": "intercept",
        "base_url": "https://www.giant-bicycles.com",
        "keywords": ["talon", "fathom", "hardtail"],
    },
    {
        "name": "Specialized",
        "url":  "https://www.specialized.com/us/en/shop/bikes/mountain-bikes/hardtail-mountain-bikes",
        "type": "intercept",
        "base_url": "https://www.specialized.com",
        "keywords": ["rockhopper", "hardrock", "hardtail"],
    },
    {
        "name": "Marin",
        "url":  "https://www.marinbikes.com/bikes/mountain/hardtail",
        "type": "intercept",
        "base_url": "https://www.marinbikes.com",
        "keywords": ["bobcat", "hardtail", "trail"],
    },
    {
        "name": "REI",
        "url":  "https://www.rei.com/c/hardtail-mountain-bikes?r=q%3A27.5",
        "type": "dom",
        "base_url": "https://www.rei.com",
        "keywords": None,
    },
    {
        "name": "Pinkbike Deals",
        "url":  "https://www.pinkbike.com/product/deals/",
        "type": "pinkbike_deals",
        "base_url": "https://www.pinkbike.com",
        "keywords": ["mountain", "hardtail", "mtb", "27.5"],
    },
    {
        "name": "Pinkbike Buy/Sell",
        "url":  (
            "https://www.pinkbike.com/buysell/list/"
            "?q=hardtail+27.5+small&cat=2&minprice=200&maxprice=900"
            "&condition=2&country_id=1"
        ),
        "type": "pinkbike_buysell",
        "base_url": "https://www.pinkbike.com",
        "keywords": None,
    },
    {
        # Local Michigan shop — Lightspeed eCom platform
        "name": "SweetWater Bicycles",
        "url":  "https://www.sweetwaterbicycles.com/bikes/mountain/",
        "type": "dom",
        "base_url": "https://www.sweetwaterbicycles.com",
        "keywords": None,
    },
    {
        # Local Michigan shop — custom platform, product list page
        "name": "Trailhead Cycling",
        "url":  "https://www.trailheadcycling.com/product-list/bikes-1000/mountain-1006/",
        "type": "dom",
        "base_url": "https://www.trailheadcycling.com",
        "keywords": None,
    },
    {
        # Miami, FL — ships nationwide, Vue.js SPA needs Playwright
        "name": "88 Cycling",
        "url":  "https://88cycling.com/products",
        "type": "dom",
        "base_url": "https://88cycling.com",
        "keywords": None,
    },
]

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────

def log(msg: str):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

# ─────────────────────────────────────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen_urls": [], "last_run": None, "deals_found": []}

def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _extract_price(text: str) -> float | None:
    m = re.search(r'\$\s*([\d,]+(?:\.\d{2})?)', text)
    return float(m.group(1).replace(',', '')) if m else None

def _extract_msrp(text: str) -> float | None:
    m = re.search(
        r'(?:msrp|was|reg(?:ular)?|orig(?:inal)?|compare)[^\d$]*\$\s*([\d,]+(?:\.\d{2})?)',
        text, re.IGNORECASE
    )
    return float(m.group(1).replace(',', '')) if m else None

# ── Target brands and models for pre-filtering ───────────────────────────────
# Expanded beyond the original brief to include strong value alternatives

_TARGET_BRANDS = {
    # Original tier 1/2
    "trek", "giant", "specialized", "marin", "kona", "cannondale",
    # Strong value alternatives worth monitoring
    "diamondback",  # Hook, Trace — excellent spec/dollar
    "norco",        # Storm — Canadian brand, great value
    "gt",           # Aggressor — solid budget hardtail
    "fuji",         # Nevada — underrated, often discounted
    "co-op",        # Co-op Cycles DRT 1.1/1.2 — REI house brand, outstanding value
    "coop",         # alternate spelling
    "liv",          # Giant's women's sub-brand — quality bikes, appropriate for teen girls
    "polygon",      # Xtrada, Cascade — incredible spec/dollar, sold via bikes.com
    "vitus",        # Nucleus — good spec, sometimes available in US
}

_TARGET_MODELS = {
    # Trek
    "marlin", "roscoe",
    # Giant / Liv
    "talon", "fathom", "tempt", "bliss",
    # Specialized
    "rockhopper", "hardrock",
    # Marin
    "bobcat", "cobia",
    # Kona  (removed honzo=29er, growler=fat bike)
    "kahuna", "lava dome",
    # Cannondale
    "trail", "catalyst", "quick cx",
    # Diamondback
    "hook", "trace", "sync",
    # Norco
    "storm", "fluid",
    # GT
    "aggressor", "palomar",
    # Fuji
    "nevada", "addy",
    # Co-op Cycles
    "drt",
    # Polygon
    "xtrada", "cascade",
    # Generic hardtail keywords
    "hardtail",
}

_REJECT_WHEELS = {
    # Explicit wrong wheel sizes — 650b is SAME as 27.5, do NOT reject it
    "26\"", " 26 ", "29\"", " 29 ", "700c",
    "24\"", " 24 ", "20\"", " 20 ", "16\"",
    "27.5+",   # plus-size / 3.0" tire — different geometry, not ideal for beginner
}

_REJECT_TYPES = {
    "full suspension", "full-suspension", "e-bike", "ebike", "electric bike",
    "fat bike", "fatbike", "fat-bike",
    "dirt jump", "dirtjump",
    "bmx", "pump track",
    "kids bike", "children", "youth bike",
    "24 lite", "jr ", "junior",
    "downhill", "enduro",   # too aggressive for beginner
    "cargo", "gravel",      # wrong category
}

# Component quality reference for Claude's criteria prompt (not used in pre-filter)
# Acceptable drivetrains: Shimano Altus 1x, Alivio 1x, Deore, SLX, XT, CUES
#                         SRAM NX, SX, GX, MicroShift Advent 1x
# Reject: any 3x drivetrain regardless of brand
# Acceptable brakes: Shimano MT200/M315/M395 hydraulic, Tektro HD-E350/E500 hydraulic
#                    Shimano mechanical disc (acceptable on budget builds)
# Reject: rim brakes, v-brakes


def _pre_filter(listing: dict) -> tuple[bool, str]:
    """
    Fast rule-based pre-filter before calling Claude.
    Returns (should_skip, reason).

    Key principle: when in doubt, KEEP it and let Claude decide.
    Only reject things that are unambiguously wrong.
    """
    title    = (listing.get("title", "") or "").lower()
    specs    = (listing.get("specs", "") or "").lower()
    size_raw = (listing.get("size",  "") or "").lower()
    combined = title + " " + specs

    # Hard reject: wrong bike type (unambiguous)
    if any(t in combined for t in _REJECT_TYPES):
        return True, "wrong type"

    # Hard reject: wrong wheel size — only based on TITLE, not full specs
    # (specs may mention other sizes in comparisons or accessory info)
    if any(w in title for w in _REJECT_WHEELS):
        return True, "wrong wheel size in title"

    # Hard reject: wrong frame size — ONLY use the extracted size field
    # Do NOT search combined text — listing may say "also available in M, L"
    if size_raw in ("medium", "large", "xl", "xxl", "xs", "x-small"):
        return True, f"wrong size ({size_raw})"

    # Must have a recognizable brand or model name
    has_brand = any(b in combined for b in _TARGET_BRANDS)
    has_model = any(m in combined for m in _TARGET_MODELS)
    if not has_brand and not has_model:
        return True, "no recognized brand or model"

    # Price sanity check
    price = listing.get("price")
    if price and (price < 150 or price > 950):
        return True, f"price ${price:.0f} out of range"

    return False, ""

def _extract_size(text: str) -> str:
    m = re.search(
        r'\b(x-?small|xsmall|xs|small|sm|\bS\b|medium|med|\bM\b|large|\bL\b|x-?large|xlarge|xl)\b',
        text, re.IGNORECASE
    )
    if not m:
        return "unknown"
    mapping = {
        'XSMALL': 'XS', 'X-SMALL': 'XS', 'XS': 'XS',
        'SMALL': 'Small', 'SM': 'Small', 'S': 'Small',
        'MEDIUM': 'Medium', 'MED': 'Medium', 'M': 'Medium',
        'LARGE': 'Large', 'L': 'Large',
        'XLARGE': 'XL', 'X-LARGE': 'XL', 'XL': 'XL',
    }
    return mapping.get(m.group(1).upper(), m.group(1))

def _strip_html(html: str) -> str:
    return re.sub(r'<[^>]+>', ' ', html or '').strip()

def _quick_price_filter(price: float | None) -> bool:
    if price is None:
        return True
    return 150 <= price <= 950

# ─────────────────────────────────────────────────────────────────────────────
#  CLAUDE EVALUATION
# ─────────────────────────────────────────────────────────────────────────────

CRITERIA_PROMPT = """You are an experienced mountain bike expert evaluating listings for a teenage rider.

RIDER: Female, 5'5", beginner-intermediate skill, neighborhood/bike paths/light trails.
BUYER: East Lansing, MI. Will travel up to 100 miles for pickup. Will buy anything that ships.

━━━ HARD REQUIREMENTS (reject if missing) ━━━
- Frame size: Small (S) only — reject XS, Medium, Large, XL
- Wheel size: 27.5" or 650b (same thing) — reject 26", 29", 24", 27.5+
- Hardtail only (front fork suspension, rigid rear)
- Disc brakes (hydraulic preferred, mechanical disc acceptable)
- Aluminum or carbon frame (no steel/hi-ten on new bikes at this price)
- Reputable brand (see brand tiers below)

━━━ BRAND TIERS ━━━
Tier 1 — Best value targets:
  Trek (Marlin 5), Giant (Talon 2), Specialized (Rockhopper), Marin (Bobcat Trail 5)

Tier 2 — Strong alternatives:
  Trek (Marlin 4), Giant (Talon 3/4), Marin (Bobcat Trail 4), Kona, Cannondale (Trail, Catalyst)
  Diamondback (Hook, Trace) — excellent spec/dollar, often deeply discounted
  Norco (Storm) — well-respected Canadian brand, great value hardtails
  GT (Aggressor) — solid entry hardtail, good deals available
  Co-op Cycles DRT 1.1/1.2 — REI house brand, outstanding value ($699 MSRP, hydraulic brakes, 1x10)
  Fuji (Nevada) — underrated, often discounted significantly
  Polygon (Xtrada, Cascade) — exceptional spec/dollar, some models have Deore at $600

Tier 3 — Accept only if deeply discounted or exceptional spec:
  Liv (Giant's women's sub-brand) — Tempt, Bliss are quality but verify they fit teen rider
  Other reputable brands with verifiable components

Reject outright: Mongoose, Huffy, Kent, Hyper, Pacific, Walmart/Target brands, unknown brands

━━━ DRIVETRAIN ━━━
Best: Shimano Deore 1x10/1x11/1x12, SLX, XT, CUES; SRAM NX/GX 1x
Good: Shimano Alivio 1x, Altus 1x, MicroShift Advent 1x — acceptable on budget builds
Acceptable: Shimano mechanical with 1x setup
Reject: ANY 3x drivetrain (3x7, 3x8, 3x21-speed) — outdated, harder to maintain

━━━ BRAKES ━━━
Best: Shimano MT200, M315, M395, M446 hydraulic; Tektro HD-E350/E500 hydraulic
Good: Shimano mechanical disc (Acera BR-M315)
Reject: Rim brakes, V-brakes

━━━ BUDGET & VALUE ━━━
- Ideal price: $400–$650
- Good deal: $651–$750 only if spec justifies (hydraulic brakes + 1x drivetrain)
- Acceptable stretch: $751–$900 ONLY if genuinely upgrade-tier (Deore/SLX, hydraulic, 2024+)
- Clearance/prior-year bikes: excellent opportunity — a 2023 Tier 1 bike at 30% off is a great deal
- Used bikes: score highly if condition is stated as good/excellent and price is 40-60% of MSRP
- Red flag: paying entry-level prices for entry-level spec (e.g., $750 for 3x mechanical)

━━━ SCORING GUIDANCE ━━━
9-10: Exceptional — Tier 1 bike, right size/wheels, hydraulic+1x, at or below ideal price
7-8:  Good deal — correct spec, right size, reasonable price, worth buying
5-6:  Fair — passes requirements but price/spec ratio is just OK
3-4:  Marginal — meets minimums but overpriced or spec is borderline
1-2:  Poor value — technically passes requirements but not worth it
0:    Reject — fails a hard requirement

SIZE NOTE: If size is ambiguous or not confirmed as Small/S, score conservatively and flag it."""


def evaluate_listing(listing: dict) -> dict:
    if not ANTHROPIC_KEY:
        return _rule_based_score(listing)

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    local_note = ""
    if listing.get("local_only"):
        local_note = f"\nNOTE: This is a LOCAL PICKUP listing. Buyer is in {BUYER_LOCATION}, max 100 mile travel."

    prompt = f"""{CRITERIA_PROMPT}
{local_note}
---
LISTING:
Source: {listing.get('source')}
Title: {listing.get('title')}
Price: ${listing.get('price', 'unknown')}
MSRP: {f"${listing['msrp']:.0f}" if listing.get('msrp') else 'not listed'}
Size available: {listing.get('size', 'unknown')}
Specs/Description: {listing.get('specs', 'not provided')[:400]}
URL: {listing.get('url', '')}
---
Respond ONLY with valid JSON:
{{
  "score": <1-10>,
  "verdict": "<good_deal|fair|skip|reject>",
  "reason": "<one clear sentence>",
  "reject": <true|false>,
  "size_confirmed": <true|false>
}}
Be strict. 7+ only for genuine value."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        log(f"  Claude eval error: {e}")

    return _rule_based_score(listing)


def _rule_based_score(listing: dict) -> dict:
    title = (listing.get('title', '') + ' ' + listing.get('specs', '')).lower()
    price = listing.get('price', 9999) or 9999

    if any(x in title for x in ['3x', 'rim brake', 'v-brake']):
        return {"score": 0, "verdict": "reject", "reason": "3x drivetrain or rim brakes", "reject": True, "size_confirmed": False}

    score = 5
    if price <= 650:   score += 2
    elif price <= 750: score += 1
    elif price > 900:  score -= 2

    if any(m in title for m in ['bobcat trail 5', 'marlin 5', 'talon 2', 'rockhopper']):
        score += 2
    elif any(m in title for m in ['talon 3', 'talon 4', 'marlin 4', 'bobcat trail 4']):
        score += 1

    if any(x in title for x in ['deore', 'slx', 'xt ', 'cues', 'hydraulic']): score += 1
    if '1x' in title or 'single' in title: score += 1

    score = max(0, min(10, score))
    return {
        "score": score,
        "verdict": "good_deal" if score >= 7 else "fair" if score >= 5 else "skip",
        "reason": "Rule-based (Claude unavailable)",
        "reject": False,
        "size_confirmed": False,
    }

# ─────────────────────────────────────────────────────────────────────────────
#  SHOPIFY API SCRAPER (The Pro's Closet, Jenson USA)
# ─────────────────────────────────────────────────────────────────────────────

def _shopify_fetch(shop_url: str, collection_slugs: list, source: str) -> list[dict]:
    """
    Fetch products from a Shopify store's public collection JSON endpoint.
    Tries each slug in collection_slugs until one returns results.
    No browser needed — pure HTTP.
    """
    listings = []
    headers  = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept":     "application/json",
    }

    # Find the first working collection slug
    working_slug = None
    for slug in collection_slugs:
        test_url = f"{shop_url}/collections/{slug}/products.json?limit=1"
        try:
            req = urllib.request.Request(test_url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read())
            if data.get("products") is not None:
                working_slug = slug
                break
        except Exception:
            continue

    if not working_slug:
        log(f"  {source}: no working Shopify collection found — skipping")
        return []

    page = 1
    while page <= 5:  # max 5 pages × 250 = 1250 products
        url = f"{shop_url}/collections/{working_slug}/products.json?limit=250&page={page}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read())
        except Exception as e:
            log(f"  {source} Shopify fetch error: {e}")
            break

        products = data.get("products", [])
        if not products:
            break

        for product in products:
            title   = product.get("title", "")
            handle  = product.get("handle", "")
            prod_url = f"{shop_url}/products/{handle}"
            body    = _strip_html(product.get("body_html", ""))
            variants = product.get("variants", [])

            # Find the lowest available price
            avail_prices = [
                float(v.get("price", 0)) for v in variants
                if v.get("available", True) and float(v.get("price", 0)) > 0
            ]
            if not avail_prices:
                avail_prices = [float(v.get("price", 0)) for v in variants if float(v.get("price", 0)) > 0]

            price = min(avail_prices) if avail_prices else None
            if not _quick_price_filter(price):
                continue

            # Find available sizes from variant options
            size_options = []
            for variant in variants:
                if not variant.get("available", True):
                    continue
                for opt_key in ["option1", "option2", "option3"]:
                    opt_val = variant.get(opt_key, "")
                    if opt_val and _extract_size(opt_val) != "unknown":
                        size_options.append(opt_val)

            size_str = ", ".join(sorted(set(size_options))) if size_options else "unknown"
            specs    = f"{body[:400]}" if body else ""

            listings.append({
                "source":     source,
                "title":      title[:120],
                "price":      price,
                "msrp":       _extract_msrp(body),
                "size":       size_str,
                "specs":      specs,
                "url":        prod_url,
                "local_only": False,
            })

        page += 1

    log(f"  {source} (Shopify API): {len(listings)} product(s) in budget range")
    return listings


# ─────────────────────────────────────────────────────────────────────────────
#  NETWORK INTERCEPTION SCRAPER (brand sites)
# ─────────────────────────────────────────────────────────────────────────────

def _scrape_with_interception(page, source_config: dict) -> list[dict]:
    """
    Navigate to a brand site page and intercept any JSON API responses
    that look like product listings. Falls back to DOM extraction if no
    API responses are captured.
    """
    source   = source_config["name"]
    url      = source_config["url"]
    base_url = source_config["base_url"]
    keywords = source_config.get("keywords", [])

    captured_products = []

    def handle_response(response):
        """Capture JSON responses that look like product arrays."""
        try:
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type:
                return
            if response.status != 200:
                return
            # Only intercept API-looking URLs, not page HTML
            resp_url = response.url.lower()
            if any(x in resp_url for x in ["api", "product", "catalog", "search", "item", "bike"]):
                body = response.json()
                # Look for arrays of product-like objects
                candidates = []
                if isinstance(body, list):
                    candidates = body
                elif isinstance(body, dict):
                    for key in ["products", "items", "results", "bikes", "data", "records"]:
                        if isinstance(body.get(key), list):
                            candidates = body[key]
                            break
                for item in candidates:
                    if isinstance(item, dict) and any(
                        k in item for k in ["name", "title", "productName", "model"]
                    ):
                        captured_products.append(item)
        except Exception:
            pass

    listings = []
    try:
        page.on("response", handle_response)
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)

        if captured_products:
            log(f"  {source}: {len(captured_products)} product(s) from API interception")
            for item in captured_products:
                # Normalize various API response shapes
                title = (item.get("name") or item.get("title") or
                         item.get("productName") or item.get("model") or "")
                price_raw = (item.get("price") or item.get("salePrice") or
                             item.get("currentPrice") or item.get("msrp") or 0)
                try:
                    price = float(str(price_raw).replace("$", "").replace(",", ""))
                except (ValueError, TypeError):
                    price = None

                if not _quick_price_filter(price):
                    continue

                href = (item.get("url") or item.get("link") or item.get("pdpUrl") or "")
                if href and not href.startswith("http"):
                    href = base_url + href

                specs = json.dumps({k: v for k, v in item.items()
                                    if k not in ["images", "image", "media"]})[:500]

                if keywords:
                    if not any(kw.lower() in (title + specs).lower() for kw in keywords):
                        continue

                listings.append({
                    "source":     source,
                    "title":      title[:120],
                    "price":      price,
                    "msrp":       None,
                    "size":       _extract_size(specs),
                    "specs":      specs,
                    "url":        href,
                    "local_only": False,
                })
        else:
            # Fallback: DOM extraction
            log(f"  {source}: no API responses captured — falling back to DOM extraction")
            listings = _scrape_dom(page, source, base_url, keywords)

    except Exception as e:
        log(f"  {source} interception error: {e}")
        listings = _scrape_dom(page, source, base_url, keywords)

    return listings


# ─────────────────────────────────────────────────────────────────────────────
#  DOM SCRAPER (fallback + REI)
# ─────────────────────────────────────────────────────────────────────────────

_EXTRACT_JS = """
(baseUrl) => {
    const results = [];
    const seen = new Set();

    // Find all text nodes containing prices, walk up to product container
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const priceNodes = [];
    let node;
    while ((node = walker.nextNode())) {
        if (/\\$\\s*[2-9]\\d{2}/.test(node.textContent)) {
            priceNodes.push(node.parentElement);
        }
    }

    for (const priceEl of priceNodes) {
        let container = priceEl;
        let link = null;
        for (let i = 0; i < 7; i++) {
            if (!container) break;
            link = container.querySelector('a[href]');
            if (link) break;
            container = container.parentElement;
        }
        if (!link) continue;

        let href = link.href || '';
        if (!href || href === window.location.href || href === '#') continue;
        if (seen.has(href)) continue;
        seen.add(href);

        const text = (container || priceEl).innerText || '';
        const lines = text.split('\\n').map(l => l.trim()).filter(l => l.length > 2);
        const title = lines[0] || link.innerText?.trim() || '';

        const priceMatch = text.match(/\\$\\s*([\\d,]+(?:\\.\\d{2})?)/);
        const price = priceMatch ? parseFloat(priceMatch[1].replace(',', '')) : null;
        if (!price || price < 150 || price > 2000) continue;

        results.push({
            title: title.substring(0, 150),
            price: price,
            url: href,
            text: text.substring(0, 700),
        });
    }
    return results;
}
"""

def _scrape_dom(page, source: str, base_url: str,
                keywords: list | None = None) -> list[dict]:
    listings = []
    try:
        products = page.evaluate(_EXTRACT_JS, base_url)
        log(f"  {source} (DOM): {len(products)} price elements found")
        for p in products:
            if not _quick_price_filter(p.get("price")):
                continue
            text = p.get("text", "")
            if keywords:
                if not any(kw.lower() in text.lower() for kw in keywords):
                    continue
            listings.append({
                "source":     source,
                "title":      p.get("title", "")[:120],
                "price":      p.get("price"),
                "msrp":       _extract_msrp(text),
                "size":       _extract_size(text),
                "specs":      text[:500],
                "url":        p.get("url", ""),
                "local_only": False,
            })
    except Exception as e:
        log(f"  {source} DOM extraction error: {e}")
    return listings


# ─────────────────────────────────────────────────────────────────────────────
#  PINKBIKE SCRAPERS
# ─────────────────────────────────────────────────────────────────────────────

def _scrape_pinkbike_deals(page, source_config: dict) -> list[dict]:
    """Scrape Pinkbike's curated deals page."""
    listings = []
    keywords = source_config.get("keywords", [])
    try:
        page.goto(source_config["url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)

        # Pinkbike deals are usually in deal cards/rows
        cards = page.query_selector_all(
            '.deal-item, [class*="deal"], [class*="product-deal"], '
            'article, .buysell-item, [class*="item-row"]'
        )

        if not cards:
            # Fall back to DOM extraction
            return _scrape_dom(page, "Pinkbike Deals", "https://www.pinkbike.com", keywords)

        for card in cards:
            try:
                text = card.inner_text().strip()
                if not text:
                    continue

                if keywords and not any(kw.lower() in text.lower() for kw in keywords):
                    continue

                link_el = card.query_selector('a')
                href = link_el.get_attribute('href') if link_el else ''
                if href and not href.startswith('http'):
                    href = f"https://www.pinkbike.com{href}"

                price = _extract_price(text)
                if not _quick_price_filter(price):
                    continue

                lines = [l.strip() for l in text.split('\n') if l.strip()]
                title = lines[0][:120] if lines else text[:80]

                listings.append({
                    "source":     "Pinkbike Deals",
                    "title":      title,
                    "price":      price,
                    "msrp":       _extract_msrp(text),
                    "size":       _extract_size(text),
                    "specs":      text[:500],
                    "url":        href,
                    "local_only": False,
                })
            except Exception:
                continue

    except Exception as e:
        log(f"  Pinkbike Deals error: {e}")
    return listings


def _scrape_pinkbike_buysell(page, source_config: dict) -> list[dict]:
    """
    Scrape Pinkbike Buy/Sell listings near East Lansing, MI.
    Flags listings as local_only if no shipping mentioned.
    """
    listings = []
    try:
        page.goto(source_config["url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)

        # Pinkbike buy/sell uses table rows or list items
        items = page.query_selector_all(
            '.bsitem, [class*="buysell-item"], [class*="bsitem"], '
            'tr[class*="item"], .item-cell, [id*="item"]'
        )

        if not items:
            items = page.query_selector_all('table tr, .buysell-results tr')

        if not items:
            return _scrape_dom(page, "Pinkbike Buy/Sell", "https://www.pinkbike.com", None)

        for item in items:
            try:
                text = item.inner_text().strip()
                if not text or len(text) < 10:
                    continue

                link_el = item.query_selector('a[href*="buysell"]')
                if not link_el:
                    link_el = item.query_selector('a')
                href = link_el.get_attribute('href') if link_el else ''
                if href and not href.startswith('http'):
                    href = f"https://www.pinkbike.com{href}"

                price = _extract_price(text)
                if not _quick_price_filter(price):
                    continue

                lines = [l.strip() for l in text.split('\n') if l.strip()]
                title = lines[0][:120] if lines else text[:80]

                # Check if ships or local only
                text_lower = text.lower()
                ships = any(w in text_lower for w in ["ship", "ships", "shipping", "will ship"])
                local_only = not ships

                # Extract location if mentioned
                location_note = ""
                loc_match = re.search(r'\b([A-Z][a-z]+(?:,\s*[A-Z]{2})?)\b', text)
                if loc_match:
                    location_note = f" [Location: {loc_match.group()}]"

                listings.append({
                    "source":     "Pinkbike Buy/Sell",
                    "title":      title + location_note,
                    "price":      price,
                    "msrp":       None,
                    "size":       _extract_size(text),
                    "specs":      text[:500],
                    "url":        href,
                    "local_only": local_only,
                })
            except Exception:
                continue

    except Exception as e:
        log(f"  Pinkbike Buy/Sell error: {e}")
    return listings


# ─────────────────────────────────────────────────────────────────────────────
#  EMAIL
# ─────────────────────────────────────────────────────────────────────────────

def send_alert(deals: list[dict]):
    if not EMAIL_PASSWORD:
        log("  No EMAIL_APP_PASSWORD — skipping email.")
        return

    deal_rows = ""
    for d in deals:
        listing    = d["listing"]
        evaluation = d["evaluation"]
        score_color = "#16a34a" if evaluation["score"] >= 8 else "#d97706"
        msrp_text   = (f' <span style="color:#9ca3af;text-decoration:line-through">'
                       f'${listing["msrp"]:.0f} MSRP</span>'
                       if listing.get("msrp") else "")
        local_badge = ""
        if listing.get("local_only"):
            local_badge = ('<span style="background:#fef3c7;color:#92400e;padding:2px 8px;'
                          'border-radius:12px;font-size:11px;margin-left:6px;">📍 Local pickup</span>')
        else:
            local_badge = ('<span style="background:#d1fae5;color:#065f46;padding:2px 8px;'
                          'border-radius:12px;font-size:11px;margin-left:6px;">📦 Ships</span>')

        deal_rows += f"""
        <div style="background:#fff;border:1px solid #e5e7eb;border-left:4px solid {score_color};
                    border-radius:8px;padding:20px;margin-bottom:16px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;">
            <div style="flex:1;">
              <div style="font-size:16px;font-weight:600;color:#111827;">{listing['title']}</div>
              <div style="font-size:12px;color:#6b7280;margin-top:2px;">
                {listing['source']}{local_badge}
              </div>
            </div>
            <div style="text-align:right;flex-shrink:0;">
              <div style="font-size:20px;font-weight:700;color:#111827;">
                ${listing['price']:.0f}{msrp_text}
              </div>
              <div style="font-size:12px;font-weight:600;color:{score_color};">
                ★ {evaluation['score']}/10 — {evaluation['verdict'].replace('_',' ').title()}
              </div>
            </div>
          </div>
          <div style="font-size:13px;color:#374151;margin:10px 0 8px;">
            <strong>Why it's a deal:</strong> {evaluation['reason']}
          </div>
          <div style="font-size:12px;color:#6b7280;margin-bottom:12px;">
            Size: <strong>{listing['size']}</strong>
          </div>
          <a href="{listing['url']}" style="display:inline-block;background:#6366f1;color:#fff;
             padding:8px 18px;border-radius:6px;text-decoration:none;font-size:13px;font-weight:500;">
            View Listing →
          </a>
        </div>"""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:600px;margin:32px auto;padding:0 16px;">
    <div style="background:linear-gradient(135deg,#4f46e5,#7c3aed);border-radius:12px;
                padding:28px;margin-bottom:24px;text-align:center;">
      <div style="font-size:28px;margin-bottom:6px;">🚲</div>
      <h1 style="color:#fff;font-size:22px;font-weight:700;margin:0 0 6px;">Bike Deal Alert</h1>
      <p style="color:rgba(255,255,255,0.8);font-size:14px;margin:0;">
        {len(deals)} new deal{'s' if len(deals) != 1 else ''} found matching your criteria
      </p>
    </div>
    {deal_rows}
    <div style="text-align:center;font-size:12px;color:#9ca3af;padding:16px 0 32px;">
      Bike Deal Finder · Checked {datetime.now().strftime('%b %d at %I:%M %p')} ·
      Near {BUYER_LOCATION} ({MAX_TRAVEL_MILES}mi radius for pickup)
    </div>
  </div>
</body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🚲 {len(deals)} Bike Deal{'s' if len(deals) != 1 else ''} Found!"
    msg["From"]    = EMAIL_ADDRESS
    msg["To"]      = ", ".join(NOTIFY_EMAILS)
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP("smtp.mail.me.com", 587) as smtp:
            smtp.starttls()
            smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            smtp.sendmail(EMAIL_ADDRESS, NOTIFY_EMAILS, msg.as_string())
        log("  Email sent.")
    except Exception as e:
        log(f"  Email error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log("═══ Bike Deal Finder v2 — run started ═══")

    state     = load_state()
    seen_urls = set(state.get("seen_urls", []))
    new_deals = []

    # ── Phase 1: Shopify API sources (no browser needed) ──────────────────────
    for source in SHOPIFY_SOURCES:
        log(f"Checking: {source['name']} (Shopify API)")
        listings = _shopify_fetch(source["shop_url"], source["collection_slugs"], source["name"])
        for listing in _evaluate_listings(listings, seen_urls):
            new_deals.append(listing)

    # ── Phase 2: Playwright sources ───────────────────────────────────────────
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--disable-infobars", "--no-first-run"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        _stealth.apply_stealth_sync(ctx)

        for source in PLAYWRIGHT_SOURCES:
            log(f"Checking: {source['name']}")
            try:
                page = ctx.new_page()
                src_type = source["type"]

                if src_type == "intercept":
                    listings = _scrape_with_interception(page, source)
                elif src_type == "pinkbike_deals":
                    listings = _scrape_pinkbike_deals(page, source)
                elif src_type == "pinkbike_buysell":
                    listings = _scrape_pinkbike_buysell(page, source)
                else:  # dom
                    page.goto(source["url"], wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(4000)
                    listings = _scrape_dom(page, source["name"], source["base_url"],
                                           source.get("keywords"))

                page.close()
            except Exception as e:
                log(f"  ERROR on {source['name']}: {e}")
                listings = []

            for deal in _evaluate_listings(listings, seen_urls):
                new_deals.append(deal)

            time.sleep(2)

        browser.close()

    # ── Save state ─────────────────────────────────────────────────────────────
    state["seen_urls"]   = list(seen_urls)[-3000:]
    state["last_run"]    = datetime.now(timezone.utc).isoformat()
    state["deals_found"] = state.get("deals_found", []) + [
        {
            "title":    d["listing"].get("title"),
            "price":    d["listing"].get("price"),
            "source":   d["listing"].get("source"),
            "score":    d["evaluation"].get("score"),
            "reason":   d["evaluation"].get("reason"),
            "url":      d["listing"].get("url"),
            "found_at": datetime.now(timezone.utc).isoformat(),
        }
        for d in new_deals
    ]
    save_state(state)

    if new_deals:
        log(f"Sending alert for {len(new_deals)} deal(s)...")
        send_alert(new_deals)
    else:
        log("No new deals found. No email sent.")

    log("═══ Run complete ═══\n")


def _evaluate_listings(listings: list[dict], seen_urls: set) -> list[dict]:
    """
    Evaluate a batch of listings against criteria.
    Pre-filters obvious rejects before calling Claude to minimize API cost.
    Deduplicates by URL within this batch.
    Returns deals scoring MIN_SCORE_TO_ALERT or higher.
    """
    deals    = []
    batch_seen = set()  # dedup within this source's batch

    pre_filtered = 0
    for listing in listings:
        url = listing.get("url", "")

        # Skip already-seen URLs (across all runs)
        if url and url in seen_urls:
            continue

        # Dedup within current batch (same product, multiple variants)
        if url and url in batch_seen:
            continue

        if not listing.get("title") or len(listing["title"]) < 5:
            continue

        # ── Pre-filter: skip obvious rejects without calling Claude ──────────
        skip, reason = _pre_filter(listing)
        if skip:
            pre_filtered += 1
            if url:
                seen_urls.add(url)
                batch_seen.add(url)
            continue

        # ── Claude evaluation ─────────────────────────────────────────────────
        try:
            evaluation = evaluate_listing(listing)
        except Exception as e:
            log(f"    Eval error: {e}")
            continue

        score  = evaluation.get("score", 0)
        reject = evaluation.get("reject", False)
        log(f"    [{score}/10] {listing.get('title', '?')[:60]} — {evaluation.get('verdict', '?')}")

        if url:
            seen_urls.add(url)
            batch_seen.add(url)

        if not reject and score >= MIN_SCORE_TO_ALERT:
            log(f"    ★ DEAL: {listing.get('title', '?')}")
            deals.append({"listing": listing, "evaluation": evaluation})

    if pre_filtered:
        log(f"    ({pre_filtered} listing(s) pre-filtered — saved {pre_filtered} Claude calls)")

    return deals


if __name__ == "__main__":
    main()
