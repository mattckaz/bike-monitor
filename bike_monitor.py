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

# GitHub dashboard deployment
GITHUB_TOKEN   = os.environ.get("GITHUB_TOKEN", os.environ.get("GH_TOKEN", ""))
DEPLOY_TOKEN   = os.environ.get("GH_TOKEN", GITHUB_TOKEN)
DASHBOARD_REPO = "mattckaz/bike-status"
DASHBOARD_URL  = "https://mattckaz.github.io/bike-status/"

# Location for Pinkbike buy/sell proximity context
BUYER_ZIP        = "48823"    # East Lansing, MI
BUYER_LOCATION   = "East Lansing, MI"
MAX_TRAVEL_MILES = 100

STATE_FILE  = Path(__file__).parent / "bike_state.json"
LOG_FILE    = Path(__file__).parent / "bike_monitor.log"
STATUS_FILE = Path(__file__).parent / "bike_status.html"

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
    # ── Confirmed working — verified inventory ────────────────────────────────
    {
        "name":             "The Pro's Closet",
        "shop_url":         "https://www.theproscloset.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes"],
    },
    {
        "name":             "Bicycle Warehouse",
        "shop_url":         "https://bicyclewarehouse.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes", "bikes"],
    },
    {
        "name":             "Incycle",
        "shop_url":         "https://www.incycle.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes", "bikes"],
    },
    {
        "name":             "Colorado Cyclist",
        "shop_url":         "https://www.coloradocyclist.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes", "bikes"],
    },
    {
        "name":             "BikesOnline",
        "shop_url":         "https://www.bikesonline.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes", "hardtail"],
    },
    {
        "name":             "Kona (Direct)",
        "shop_url":         "https://konaworld.com",
        "collection_slugs": ["mountain-bikes", "hardtail-mountain-bikes", "bikes"],
    },
    {
        "name":             "Fuji (Direct)",
        "shop_url":         "https://www.fujibikes.com",
        "collection_slugs": ["mountain", "sale", "all-bikes"],
    },
    {
        "name":             "GT Bicycles (Direct)",
        "shop_url":         "https://www.gtbicycles.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes", "bikes"],
    },
    {
        "name":             "Diamondback (Direct)",
        "shop_url":         "https://www.diamondbackbikes.com",
        "collection_slugs": ["mountain-bikes", "bikes"],
    },
    # ── Low/no inventory right now but watch for restocks ─────────────────────
    {
        "name":             "Worldwide Cyclery",
        "shop_url":         "https://www.worldwidecyclery.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes"],
    },
    {
        "name":             "Fanatik Bike",
        "shop_url":         "https://www.fanatikbike.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes"],
    },
    {
        "name":             "Cambria Bike",
        "shop_url":         "https://www.cambriabike.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes"],
    },
    {
        "name":             "Mike's Bikes",
        "shop_url":         "https://www.mikesbikes.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes"],
    },
    {
        "name":             "ERIK'S Bike Shop",
        "shop_url":         "https://www.eriksbikeshop.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes"],
    },
    {
        "name":             "Velomine",
        "shop_url":         "https://www.velomine.com",
        "collection_slugs": ["mountain-bikes", "hardtail", "bikes"],
    },
    {
        "name":             "Ari Bikes",
        "shop_url":         "https://www.aribikes.com",
        "collection_slugs": ["hardtail-mountain-bikes", "mountain-bikes", "bikes"],
    },
    # ── New additions ─────────────────────────────────────────────────────────
    {
        # DFW-based chain, carries Trek — confirmed has Marlin 4 Gen 3 in stock
        "name":             "Bike Mart",
        "shop_url":         "https://www.bikemart.com",
        "collection_slugs": ["bikes", "mountain-bikes", "trek"],
    },
    # REMOVED (no longer Shopify or API blocked):
    # Jenson USA (308 redirect), Universal Cycles (404),
    # Competitive Cyclist (403), Backcountry (403), Bicycle Blue Book (404)
]

PLAYWRIGHT_SOURCES = [
    # ── Brand sites — clearance pages more reliable + higher deal probability ──
    {
        "name":     "Trek Sale",
        "url":      "https://www.trekbikes.com/us/en_US/sale_and_clearance/bikes/mountain_bikes/",
        "type":     "dom",
        "base_url": "https://www.trekbikes.com",
        "keywords": ["marlin", "roscoe", "hardtail"],
        "wait_ms":  6000,
    },
    {
        "name":     "Giant Clearance",
        "url":      "https://www.giant-bicycles.com/us/clearance-sale",
        "type":     "dom",
        "base_url": "https://www.giant-bicycles.com",
        "keywords": ["talon", "fathom", "hardtail"],
        "wait_ms":  6000,
    },
    {
        "name":     "Specialized Sale",
        "url":      "https://www.specialized.com/us/en/shop/sale/bikes",
        "type":     "dom",
        "base_url": "https://www.specialized.com",
        "keywords": ["rockhopper", "hardrock", "hardtail"],
        "wait_ms":  7000,
    },
    {
        "name":     "Marin",
        "url":      "https://www.marinbikes.com/bikes/mountain/hardtail",
        "type":     "dom",
        "base_url": "https://www.marinbikes.com",
        "keywords": ["bobcat", "hardtail", "trail"],
        "wait_ms":  5000,
    },
    # ── Pinkbike ─────────────────────────────────────────────────────────────
    {
        "name":     "Pinkbike Deals",
        "url":      "https://www.pinkbike.com/product/deals/",
        "type":     "pinkbike_deals",
        "base_url": "https://www.pinkbike.com",
        "keywords": ["mountain", "hardtail", "mtb", "27.5"],
        "wait_ms":  4000,
    },
    {
        # Search by size + wheel
        "name":     "Pinkbike Buy/Sell (27.5 S/M)",
        "url":      "https://www.pinkbike.com/buysell/list/?q=hardtail+27.5&cat=2&minprice=200&maxprice=900&country_id=1",
        "type":     "pinkbike_buysell",
        "base_url": "https://www.pinkbike.com",
        "keywords": None,
        "wait_ms":  5000,
    },
    {
        # Search by Tier 1 model names specifically
        "name":     "Pinkbike Buy/Sell (Marlin/Talon/Rockhopper)",
        "url":      "https://www.pinkbike.com/buysell/list/?q=marlin+OR+talon+OR+rockhopper+OR+bobcat&cat=2&minprice=200&maxprice=900&country_id=1",
        "type":     "pinkbike_buysell",
        "base_url": "https://www.pinkbike.com",
        "keywords": None,
        "wait_ms":  5000,
    },
    # ── Local Michigan shops ──────────────────────────────────────────────────
    {
        "name":     "SweetWater Bicycles",
        "url":      "https://www.sweetwaterbicycles.com/bikes/mountain/",
        "type":     "dom",
        "base_url": "https://www.sweetwaterbicycles.com",
        "keywords": None,
        "wait_ms":  4000,
    },
    {
        "name":     "Trailhead Cycling",
        "url":      "https://www.trailheadcycling.com/product-list/bikes-1000/mountain-1006/",
        "type":     "dom",
        "base_url": "https://www.trailheadcycling.com",
        "keywords": None,
        "wait_ms":  4000,
    },
    # ── Other online retailers ────────────────────────────────────────────────
    {
        "name":     "Dick's Sporting Goods",
        "url":      "https://www.dickssportinggoods.com/c/hardtail-mountain-bikes",
        "type":     "dom",
        "base_url": "https://www.dickssportinggoods.com",
        "keywords": None,
        "wait_ms":  5000,
    },
    {
        "name":     "88 Cycling",
        "url":      "https://88cycling.com/products",
        "type":     "dom",
        "base_url": "https://88cycling.com",
        "keywords": None,
        "wait_ms":  5000,
    },
    {
        "name":     "Canyon",
        "url":      "https://www.canyon.com/en-us/mountain-bikes/hardtail/",
        "type":     "dom",
        "base_url": "https://www.canyon.com",
        "keywords": ["hardtail", "27.5", "grand canyon", "stoic"],
        "wait_ms":  6000,
    },
    {
        "name":     "Batch Bicycles",
        "url":      "https://www.batchbicycles.com/products",
        "type":     "dom",
        "base_url": "https://www.batchbicycles.com",
        "keywords": ["mountain", "hardtail"],
        "wait_ms":  5000,
    },
    # ── eBay — new + used, Small + Medium ────────────────────────────────────
    {
        "name":     "eBay Used (Small)",
        "url":      (
            "https://www.ebay.com/sch/i.html"
            "?_nkw=hardtail+mountain+bike+27.5+small"
            "&_sacat=177831&LH_BIN=1&_udhi=900&LH_ItemCondition=3000&_sop=10"
        ),
        "type":     "ebay",
        "base_url": "https://www.ebay.com",
        "keywords": None,
        "wait_ms":  5000,
    },
    {
        "name":     "eBay Used (Medium)",
        "url":      (
            "https://www.ebay.com/sch/i.html"
            "?_nkw=hardtail+mountain+bike+27.5+medium"
            "&_sacat=177831&LH_BIN=1&_udhi=900&LH_ItemCondition=3000&_sop=10"
        ),
        "type":     "ebay",
        "base_url": "https://www.ebay.com",
        "keywords": None,
        "wait_ms":  5000,
    },
    {
        "name":     "eBay New (Small)",
        "url":      (
            "https://www.ebay.com/sch/i.html"
            "?_nkw=hardtail+mountain+bike+27.5+small"
            "&_sacat=177831&LH_BIN=1&_udhi=900&LH_ItemCondition=1000&_sop=10"
        ),
        "type":     "ebay",
        "base_url": "https://www.ebay.com",
        "keywords": None,
        "wait_ms":  5000,
    },
    {
        "name":     "eBay New (Medium)",
        "url":      (
            "https://www.ebay.com/sch/i.html"
            "?_nkw=hardtail+mountain+bike+27.5+medium"
            "&_sacat=177831&LH_BIN=1&_udhi=900&LH_ItemCondition=1000&_sop=10"
        ),
        "type":     "ebay",
        "base_url": "https://www.ebay.com",
        "keywords": None,
        "wait_ms":  5000,
    },
    # REMOVED: REI + REI Sale — permanently blocked from GitHub Actions IPs
]

# HTTP-only sources (no browser needed)
HTTP_SOURCES = [
    {"name": "Slickdeals", "type": "slickdeals"},
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
    "jamis",        # Trail X series — solid hardtail MTBs, respected brand
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
    # Kona (removed honzo=29er, growler=fat bike)
    "kahuna", "cinder cone", "lava dome", "mahuna",
    # Cannondale
    "trail", "catalyst", "quick cx", "habit",
    # Diamondback
    "hook", "trace", "sync", "line",
    # Norco
    "storm", "fluid",
    # GT
    "aggressor", "palomar", "zaskar",
    # Fuji
    "nevada", "addy", "rakan",
    # Co-op Cycles (REI house brand — outstanding value)
    "drt",
    # Jamis
    "trail x", "highpoint", "divide",
    # Polygon
    "xtrada", "cascade", "heist",
    # Ari Bikes (formerly Fezzari)
    "ari ", "signal peak", "wire peak",
    # Batch
    "the mountain bike", "batch",
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
    # Accept Small AND Medium — rider is 5'5" which is on the border for many brands
    # Do NOT search combined text — listing may say "also available in S, M, L"
    if size_raw in ("large", "xl", "xxl", "xs", "x-small"):
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
- Frame size: Small (S) OR Medium (M) — rider is 5'5", right on the border for many brands
  · Small is ideal for Trek, Marin; Medium may actually fit better on Giant, Specialized, Kona
  · Reject XS (too small), Large/XL/XXL (too big)
  · If Medium, note in reason that fit should be verified before buying
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
  Jamis — Trail X series are solid hardtail MTBs, Highpoint and Divide also acceptable
  Marin Bolinas Ridge — flat-bar adventure/trail bike, appropriate for this rider's use case
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

        # Strategy 1: direct JSON parse
        m = re.search(r'\{.*?\}', text, re.DOTALL)
        if m:
            raw = m.group()
            try:
                # Clean common JSON issues: trailing commas, smart quotes (by unicode escape)
                raw = raw.replace(chr(0x201c), chr(0x22)).replace(chr(0x201d), chr(0x22))
                raw = raw.replace(chr(0x2018), chr(0x27)).replace(chr(0x2019), chr(0x27))
                raw = re.sub(r',\s*([}\]])', r'\1', raw)
                return json.loads(raw)
            except json.JSONDecodeError:
                pass

        # Strategy 2: extract individual fields with regex (handles broken JSON)
        score_m   = re.search(r'"score"\s*:\s*(\d+)', text)
        verdict_m = re.search(r'"verdict"\s*:\s*"([^"]+)"', text)
        reason_m  = re.search(r'"reason"\s*:\s*"([^"]+)"', text)
        reject_m  = re.search(r'"reject"\s*:\s*(true|false)', text)
        size_m    = re.search(r'"size_confirmed"\s*:\s*(true|false)', text)
        if score_m:
            return {
                "score":          int(score_m.group(1)),
                "verdict":        verdict_m.group(1) if verdict_m else "unknown",
                "reason":         reason_m.group(1) if reason_m else "",
                "reject":         reject_m.group(1) == "true" if reject_m else False,
                "size_confirmed": size_m.group(1) == "true" if size_m else False,
            }

    except Exception as e:
        log(f"  Claude eval error: {e}")

    return _rule_based_score(listing)


def _rule_based_score(listing: dict) -> dict:
    title = (listing.get('title', '') + ' ' + listing.get('specs', '')).lower()
    price = listing.get('price', 9999) or 9999

    # Hard rejects
    if any(x in title for x in ['3x', 'rim brake', 'v-brake']):
        return {"score": 0, "verdict": "reject", "reason": "3x drivetrain or rim brakes", "reject": True, "size_confirmed": False}

    # Must have an approved brand — fallback shouldn't reward unknown brands
    if not any(b in title for b in _TARGET_BRANDS):
        return {"score": 0, "verdict": "reject", "reason": "No approved brand detected", "reject": True, "size_confirmed": False}

    # Reject clear non-MTB types only
    non_mtb = ["road bike", "cyclocross", "commuter", "city bike", "fixie", "bmx", "drop bar"]
    if any(x in title for x in non_mtb):
        return {"score": 0, "verdict": "reject", "reason": "Not a hardtail MTB", "reject": True, "size_confirmed": False}

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
#  EBAY SCRAPER
# ─────────────────────────────────────────────────────────────────────────────

def scrape_ebay(page, source_config: dict) -> list[dict]:
    """
    Scrape eBay hardtail mountain bike listings.
    Uses the universal DOM price extractor since eBay changes CSS classes frequently.
    Filters out non-bike items and eBay ghost listings.
    """
    listings = []
    try:
        page.goto(source_config["url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)

        # Use universal price-based DOM extractor — resilient to eBay class changes
        raw = _scrape_dom(page, "eBay", "https://www.ebay.com", None)

        for item in raw:
            title = item.get("title", "")
            text  = item.get("specs", "")
            href  = item.get("url", "")

            # Skip eBay ghost/promo items
            if any(x in title.lower() for x in ["shop on ebay", "results for", "sponsored"]):
                continue

            # eBay item URLs should contain /itm/ — filter navigation links
            if href and "/itm/" not in href and "ebay.com" in href:
                continue

            # Clean tracking params from URL
            if href and '?' in href:
                href = href.split('?')[0]

            item["url"]        = href
            item["source"]     = "eBay"
            item["local_only"] = False
            listings.append(item)

        log(f"  eBay: {len(listings)} listing(s) found")
    except Exception as e:
        log(f"  eBay scrape error: {e}")
    return listings


# ─────────────────────────────────────────────────────────────────────────────
#  SLICKDEALS SCRAPER (RSS feed — no browser needed)
# ─────────────────────────────────────────────────────────────────────────────

def scrape_slickdeals() -> list[dict]:
    """
    Fetch Slickdeals RSS feed for mountain bike deals.
    Pure HTTP — no Playwright needed.
    """
    listings = []
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    urls = [
        "https://slickdeals.net/newsearch.php?src=frontpage&q=mountain+bike+hardtail&rss=1&pp=20",
        "https://slickdeals.net/newsearch.php?src=frontpage&q=mountain+bike+27.5&rss=1&pp=20",
    ]
    seen = set()
    for rss_url in urls:
        try:
            req = urllib.request.Request(rss_url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                content = r.read().decode("utf-8", errors="ignore")

            # Parse RSS items simply with regex (no xml parser needed)
            items = re.findall(r'<item>(.*?)</item>', content, re.DOTALL)
            for item_xml in items:
                title_m = re.search(r'<title><!\[CDATA\[(.*?)\]\]></title>', item_xml)
                link_m  = re.search(r'<link>(.*?)</link>', item_xml)
                desc_m  = re.search(r'<description><!\[CDATA\[(.*?)\]\]></description>', item_xml)

                if not title_m:
                    continue
                title = title_m.group(1).strip()
                href  = link_m.group(1).strip() if link_m else ""
                desc  = _strip_html(desc_m.group(1)) if desc_m else ""

                if href in seen:
                    continue
                seen.add(href)

                combined = (title + " " + desc).lower()
                price = _extract_price(title) or _extract_price(desc)

                if not _quick_price_filter(price):
                    continue

                listings.append({
                    "source":     "Slickdeals",
                    "title":      title[:120],
                    "price":      price,
                    "msrp":       _extract_msrp(desc),
                    "size":       _extract_size(combined),
                    "specs":      (title + " " + desc)[:500],
                    "url":        href,
                    "local_only": False,
                })
        except Exception as e:
            log(f"  Slickdeals error: {e}")

    log(f"  Slickdeals: {len(listings)} deal(s) found")
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
    Scrape Pinkbike Buy/Sell listings.
    Uses DOM extractor since Pinkbike's CSS classes change frequently.
    Flags listings as local_only if no shipping mentioned.
    """
    listings = []
    try:
        page.goto(source_config["url"], wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)

        # Use the robust DOM price-based extractor
        raw = _scrape_dom(page, source_config["name"], "https://www.pinkbike.com", None)

        for listing in raw:
            text = listing.get("specs", "")
            text_lower = text.lower()
            ships = any(w in text_lower for w in ["ship", "ships", "shipping", "will ship"])

            listing["local_only"] = not ships
            listing["source"]     = "Pinkbike Buy/Sell"

            # Extract location if mentioned
            loc_match = re.search(r'\b([A-Z][a-z]+(?:,\s*[A-Z]{2})?)\b', text)
            if loc_match and listing.get("title"):
                listing["title"] = listing["title"]  # keep as-is, location in specs

            listings.append(listing)

        return listings

        # Legacy CSS selector approach kept as reference but not used:
        items = []
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
#  DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

def _all_sources() -> list[str]:
    return (
        [s["name"] for s in SHOPIFY_SOURCES] +
        [s["name"] for s in PLAYWRIGHT_SOURCES] +
        [s["name"] for s in HTTP_SOURCES]
    )

def write_status_page(state: dict):
    """Generate the HTML dashboard and save to STATUS_FILE."""
    now_utc       = datetime.now(timezone.utc).isoformat()
    last_run      = state.get("last_run", "")
    deals_found   = state.get("deals_found", [])
    source_checks = state.get("source_checks", {})
    total_sources = len(_all_sources())

    # Log lines
    log_html = ""
    if LOG_FILE.exists():
        lines = LOG_FILE.read_text().splitlines()[-50:]
        for line in lines:
            cls = "log-line"
            if "ERROR" in line or "error" in line:    cls += " error"
            elif "DEAL" in line or "★" in line:       cls += " success"
            elif "WARNING" in line or "bot" in line:  cls += " warn"
            elif "═══" in line:                       cls += " sep"
            log_html += f'<div class="{cls}">{line}</div>\n'
    if not log_html:
        log_html = '<div class="log-line">No activity yet.</div>'

    # Recent deals (last 20)
    recent_deals = list(reversed(deals_found[-20:])) if deals_found else []
    deals_html = ""
    if recent_deals:
        for d in recent_deals:
            score      = d.get("score", 0)
            score_color = "#16a34a" if score >= 8 else "#d97706" if score >= 6 else "#6b7280"
            found_dt   = d.get("found_at", "")
            try:
                found_label = datetime.fromisoformat(found_dt).strftime("%b %d, %Y")
            except Exception:
                found_label = found_dt[:10] if found_dt else ""
            price = d.get("price")
            price_str = f"${price:.0f}" if price else "?"
            deals_html += f"""
            <div class="deal-row">
              <div class="deal-score" style="color:{score_color}">★ {score}/10</div>
              <div class="deal-info">
                <div class="deal-title"><a href="{d.get('url','#')}" target="_blank">{d.get('title','?')[:80]}</a></div>
                <div class="deal-meta">{d.get('source','?')} · {price_str} · {found_label}</div>
                <div class="deal-reason">{d.get('reason','')}</div>
              </div>
            </div>"""
    else:
        deals_html = '<p class="no-deals">No deals found yet — monitor is actively watching.</p>'

    # Source status grid
    sources_html = ""
    for name in _all_sources():
        info    = source_checks.get(name, {})
        checked = info.get("last_checked", "")
        status  = info.get("status", "pending")
        count   = info.get("count_evaluated", 0)
        dot_cls = "dot-ok" if status == "ok" else "dot-err" if status == "error" else "dot-pending"
        try:
            checked_label = datetime.fromisoformat(checked).strftime("%I:%M %p") if checked else "–"
        except Exception:
            checked_label = "–"
        sources_html += f"""
        <div class="source-chip {dot_cls}">
          <span class="src-name">{name}</span>
          <span class="src-meta">{checked_label} · {count} evaluated</span>
        </div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="14400">
  <title>Bike Deal Finder</title>
  <style>
    :root {{
      --indigo:#6366f1;--purple:#7c3aed;
      --green:#16a34a;--green-100:#dcfce7;--green-700:#15803d;
      --amber:#d97706;--amber-100:#fef3c7;
      --gray-100:#f3f4f6;--gray-200:#e5e7eb;--gray-400:#9ca3af;
      --gray-500:#6b7280;--gray-700:#374151;--gray-800:#1f2937;--gray-900:#111827;
      --shadow:0 4px 8px -2px rgba(0,0,0,.08),0 2px 4px -2px rgba(0,0,0,.04);
    }}
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Inter",sans-serif;
          background:#eef2f7;color:var(--gray-900);min-height:100vh}}
    .hero{{background:linear-gradient(135deg,#312e81 0%,#4f46e5 55%,#7c3aed 100%);
           padding:44px 24px 64px;position:relative;overflow:hidden}}
    .hero::before{{content:'';position:absolute;top:-60px;right:-60px;width:360px;height:360px;
                  background:radial-gradient(circle,rgba(255,255,255,.07) 0%,transparent 65%);
                  border-radius:50%;pointer-events:none}}
    .hero-inner{{max-width:960px;margin:auto;position:relative;z-index:1}}
    .hero h1{{font-size:30px;font-weight:700;color:#fff;letter-spacing:-.5px;margin-bottom:8px}}
    .hero-sub{{color:rgba(255,255,255,.65);font-size:14px;margin-bottom:22px}}
    .chips{{display:flex;gap:8px;flex-wrap:wrap}}
    .chip{{display:inline-flex;align-items:center;gap:6px;background:rgba(255,255,255,.11);
           border:1px solid rgba(255,255,255,.18);color:rgba(255,255,255,.88);
           border-radius:20px;padding:5px 14px;font-size:12px;font-weight:500}}
    .live-dot{{width:7px;height:7px;background:#4ade80;border-radius:50%;
               animation:pulse 2.2s ease-in-out infinite}}
    @keyframes pulse{{0%,100%{{opacity:1;transform:scale(1)}}50%{{opacity:.5;transform:scale(1.45)}}}}
    .container{{max-width:960px;margin:-26px auto 0;padding:0 24px 56px;position:relative;z-index:2}}
    .stats-grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:28px}}
    .stat-card{{background:#fff;border-radius:14px;padding:18px 20px 16px;box-shadow:var(--shadow)}}
    .stat-label{{font-size:10.5px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;
                 color:var(--gray-400);margin-bottom:6px}}
    .stat-value{{font-size:26px;font-weight:700;color:var(--gray-900);line-height:1.1}}
    .stat-value.green{{color:var(--green)}}
    .stat-value.sm{{font-size:15px;font-weight:600}}
    .stat-sub{{font-size:11px;color:var(--gray-400);margin-top:2px}}
    .card{{background:#fff;border-radius:14px;box-shadow:var(--shadow);
           padding:20px 22px;margin-bottom:16px}}
    .section-title{{font-size:15px;font-weight:600;color:var(--gray-800);margin-bottom:4px}}
    .section-sub{{font-size:12px;color:var(--gray-400);margin-bottom:16px}}
    /* Deals */
    .deal-row{{display:flex;gap:14px;align-items:flex-start;padding:14px 0;
               border-bottom:1px solid var(--gray-100)}}
    .deal-row:last-child{{border-bottom:none}}
    .deal-score{{font-size:13px;font-weight:700;flex-shrink:0;width:52px;padding-top:2px}}
    .deal-title{{font-size:14px;font-weight:600;margin-bottom:3px}}
    .deal-title a{{color:var(--gray-900);text-decoration:none}}
    .deal-title a:hover{{color:var(--indigo);text-decoration:underline}}
    .deal-meta{{font-size:11px;color:var(--gray-400);margin-bottom:3px}}
    .deal-reason{{font-size:12px;color:var(--gray-500)}}
    .no-deals{{font-size:13px;color:var(--gray-400);font-style:italic;padding:8px 0}}
    /* Sources grid */
    .sources-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px}}
    .source-chip{{background:var(--gray-100);border-radius:8px;padding:10px 12px;
                  border-left:3px solid var(--gray-200)}}
    .source-chip.dot-ok{{border-left-color:var(--green)}}
    .source-chip.dot-err{{border-left-color:#ef4444}}
    .source-chip.dot-pending{{border-left-color:var(--gray-200)}}
    .src-name{{display:block;font-size:12px;font-weight:600;color:var(--gray-800);margin-bottom:2px}}
    .src-meta{{display:block;font-size:10px;color:var(--gray-400)}}
    /* Log */
    .log-terminal{{background:#0d1117;border-radius:8px;padding:16px;max-height:320px;
                   overflow-y:auto;font-family:"SF Mono","Cascadia Code",ui-monospace,monospace;
                   scrollbar-width:thin;scrollbar-color:#30363d transparent}}
    .log-line{{font-size:11.5px;line-height:1.75;color:#8b949e}}
    .log-line.error{{color:#f85149}}.log-line.warn{{color:#d29922}}
    .log-line.success{{color:#3fb950}}.log-line.sep{{color:#4d5566;font-weight:600}}
    .footer{{text-align:center;font-size:12px;color:var(--gray-400);padding-bottom:36px}}
    .footer a{{color:var(--indigo);text-decoration:none}}
    @media(max-width:600px){{
      .hero{{padding:28px 16px 52px}}.hero h1{{font-size:24px}}
      .container{{padding:0 16px 36px}}
      .stats-grid{{grid-template-columns:repeat(2,1fr)}}
      .sources-grid{{grid-template-columns:1fr 1fr}}
    }}
  </style>
</head>
<body>
<header class="hero">
  <div class="hero-inner">
    <h1>🚲 Bike Deal Finder</h1>
    <p class="hero-sub">Watching {total_sources} sources · Small/Medium 27.5" hardtail · $400–$900 · Near {BUYER_LOCATION}</p>
    <div class="chips">
      <span class="chip"><span class="live-dot"></span>Monitoring active</span>
      <span class="chip">Checks every 4 hours</span>
      <span class="chip">Updated <time class="local-time" data-utc="{now_utc}">–</time></span>
      <span class="chip">{len(deals_found)} deal{'s' if len(deals_found) != 1 else ''} found all-time</span>
    </div>
  </div>
</header>
<main class="container">
  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label">Deals Found</div>
      <div class="stat-value{'  green' if deals_found else ''}">{len(deals_found)}</div>
      <div class="stat-sub">all time</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Last Check</div>
      <div class="stat-value sm"><time class="local-time" data-utc="{last_run}">–</time></div>
      <div class="stat-sub">runs every 4 hours</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Sources</div>
      <div class="stat-value">{total_sources}</div>
      <div class="stat-sub">shops monitored</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Coverage</div>
      <div class="stat-value sm">22 API · {total_sources - 22} scraped</div>
      <div class="stat-sub">Shopify + Playwright</div>
    </div>
  </div>

  <div style="margin-bottom:10px">
    <div class="section-title">Deals Found</div>
    <div class="section-sub">All listings that scored {MIN_SCORE_TO_ALERT}+ / 10 — most recent first</div>
  </div>
  <div class="card">
    {deals_html}
  </div>

  <div style="margin-bottom:10px;margin-top:8px">
    <div class="section-title">Source Status</div>
    <div class="section-sub">{total_sources} sources · green = last run OK · red = error</div>
  </div>
  <div class="card">
    <div class="sources-grid">{sources_html}</div>
  </div>

  <div style="margin-bottom:10px;margin-top:8px">
    <div class="section-title">Recent Activity</div>
  </div>
  <div class="card">
    <div class="log-terminal">{log_html}</div>
  </div>
</main>
<footer class="footer">
  <a href="{DASHBOARD_URL}">{DASHBOARD_URL}</a>
  &nbsp;·&nbsp; Rider: 5\'5", beginner, East Lansing MI
  &nbsp;·&nbsp; Alert threshold: {MIN_SCORE_TO_ALERT}/10
</footer>
<script>
  document.querySelectorAll('time.local-time[data-utc]').forEach(function(el) {{
    var raw = el.dataset.utc;
    if (!raw) return;
    try {{
      var d = new Date(raw);
      if (isNaN(d)) return;
      el.textContent = d.toLocaleString(undefined, {{
        month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'
      }});
    }} catch(e) {{}}
  }});
  // Auto-scroll log to bottom
  var log = document.querySelector('.log-terminal');
  if (log) log.scrollTop = log.scrollHeight;
</script>
</body>
</html>"""

    STATUS_FILE.write_text(html)


def _gh_request(path: str, method: str = "GET", data: dict | None = None,
                token: str = "") -> tuple[int, dict]:
    url     = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"token {token or DEPLOY_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
        "Content-Type":  "application/json",
    }
    body = json.dumps(data).encode() if data else None
    req  = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def deploy_to_github():
    """Push the status page HTML to the public bike-status GitHub Pages repo."""
    if not DEPLOY_TOKEN or not DASHBOARD_REPO:
        return
    try:
        import base64
        content = base64.b64encode(STATUS_FILE.read_bytes()).decode()
        sha = ""
        status, existing = _gh_request(f"/repos/{DASHBOARD_REPO}/contents/index.html")
        if status == 200:
            sha = existing.get("sha", "")
        payload = {
            "message": f"Update dashboard {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            "content": content,
        }
        if sha:
            payload["sha"] = sha
        status, result = _gh_request(
            f"/repos/{DASHBOARD_REPO}/contents/index.html",
            method="PUT", data=payload,
        )
        commit = result.get("commit", {}).get("sha", "")[:7]
        if commit:
            log(f"  Dashboard deployed: {commit} → {DASHBOARD_URL}")
    except Exception as e:
        log(f"  Dashboard deploy error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    log("═══ Bike Deal Finder v2 — run started ═══")

    state         = load_state()
    seen_urls     = set(state.get("seen_urls", []))
    source_checks = state.get("source_checks", {})
    new_deals     = []

    def _record_source(name: str, count: int, ok: bool):
        source_checks[name] = {
            "last_checked":    datetime.now(timezone.utc).isoformat(),
            "count_evaluated": count,
            "status":          "ok" if ok else "error",
        }

    # ── Phase 1: Shopify API sources (no browser needed) ──────────────────────
    for source in SHOPIFY_SOURCES:
        log(f"Checking: {source['name']} (Shopify API)")
        try:
            listings = _shopify_fetch(source["shop_url"], source["collection_slugs"], source["name"])
            deals    = _evaluate_listings(listings, seen_urls)
            new_deals.extend(deals)
            _record_source(source["name"], len(listings), True)
        except Exception as e:
            log(f"  ERROR on {source['name']}: {e}")
            _record_source(source["name"], 0, False)

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
                elif src_type == "ebay":
                    listings = scrape_ebay(page, source)
                else:  # dom
                    wait_ms = source.get("wait_ms", 4000)
                    page.goto(source["url"], wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(wait_ms)
                    listings = _scrape_dom(page, source["name"], source["base_url"],
                                           source.get("keywords"))

                page.close()
                _record_source(source["name"], len(listings), True)
            except Exception as e:
                log(f"  ERROR on {source['name']}: {e}")
                listings = []
                _record_source(source["name"], 0, False)

            for deal in _evaluate_listings(listings, seen_urls):
                new_deals.append(deal)

            time.sleep(2)

        browser.close()

    # ── Phase 3: HTTP-only sources (no browser needed) ────────────────────────
    for source in HTTP_SOURCES:
        log(f"Checking: {source['name']}")
        try:
            if source["type"] == "slickdeals":
                listings = scrape_slickdeals()
            else:
                listings = []
            deals = _evaluate_listings(listings, seen_urls)
            new_deals.extend(deals)
            _record_source(source["name"], len(listings), True)
        except Exception as e:
            log(f"  ERROR on {source['name']}: {e}")
            _record_source(source["name"], 0, False)

    # ── Add alerted URLs to seen_urls so we never re-alert on the same deal ──
    for d in new_deals:
        url = d["listing"].get("url", "")
        if url:
            seen_urls.add(url)

    # ── Save state ─────────────────────────────────────────────────────────────
    state["seen_urls"]     = list(seen_urls)[-3000:]
    state["last_run"]      = datetime.now(timezone.utc).isoformat()
    state["source_checks"] = source_checks
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
    write_status_page(state)
    deploy_to_github()

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

    seen_urls = URLs we've already ALERTED on (sent email). These are skipped
    to prevent duplicate alerts. Everything else is re-evaluated every run so
    we catch price drops and new listings. Within-run duplicates handled by
    batch_seen.
    """
    deals      = []
    batch_seen = set()  # dedup within this source's batch only

    pre_filtered = 0
    for listing in listings:
        url = listing.get("url", "")

        # Skip URLs we've already sent an alert for
        if url and url in seen_urls:
            continue

        # Dedup within current batch (same product, multiple variants)
        if url and url in batch_seen:
            continue
        if url:
            batch_seen.add(url)

        if not listing.get("title") or len(listing["title"]) < 5:
            continue

        # ── Pre-filter: skip obvious rejects without calling Claude ──────────
        skip, reason = _pre_filter(listing)
        if skip:
            pre_filtered += 1
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

        if not reject and score >= MIN_SCORE_TO_ALERT:
            log(f"    ★ DEAL: {listing.get('title', '?')}")
            deals.append({"listing": listing, "evaluation": evaluation})

    if pre_filtered:
        log(f"    ({pre_filtered} listing(s) pre-filtered — saved {pre_filtered} Claude calls)")

    return deals


def notify_failure():
    """Send an email when the workflow fails — called by GitHub Actions on failure."""
    if not EMAIL_PASSWORD:
        print("No EMAIL_APP_PASSWORD — skipping failure email.")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = "⚠️ Bike Deal Finder — Workflow Failed"
        msg["From"]    = EMAIL_ADDRESS
        msg["To"]      = EMAIL_ADDRESS
        body = (
            "<p>The Bike Deal Finder workflow failed on GitHub Actions.</p>"
            f"<p>Check the run logs: "
            f"<a href='https://github.com/{DASHBOARD_REPO.replace('bike-status','bike-monitor')}/actions'>"
            f"github.com/mattckaz/bike-monitor/actions</a></p>"
            "<p>This is an automated alert.</p>"
        )
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP("smtp.mail.me.com", 587) as smtp:
            smtp.starttls()
            smtp.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
            smtp.sendmail(EMAIL_ADDRESS, [EMAIL_ADDRESS], msg.as_string())
        print("Failure email sent.")
    except Exception as e:
        print(f"Failed to send failure email: {e}")


if __name__ == "__main__":
    import sys
    if "--notify-failure" in sys.argv:
        notify_failure()
    else:
        main()
