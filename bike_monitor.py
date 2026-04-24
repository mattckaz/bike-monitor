#!/usr/bin/env python3
"""
Bike Deal Finder v1
Monitors REI, Jenson USA, Trek, Giant, Specialized, Marin, and The Pro's Closet
for mountain bike deals matching specific criteria for a teenage rider (5'5").

Uses Claude Haiku to intelligently evaluate each listing's value.
Alerts via email when a genuinely good deal is found.
"""

import json
import os
import re
import smtplib
import sys
import time
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
NOTIFY_EMAILS  = ["mattkaz@icloud.com", "jmartello@gmail.com"]
ANTHROPIC_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")

STATE_FILE = Path(__file__).parent / "bike_state.json"
LOG_FILE   = Path(__file__).parent / "bike_monitor.log"

MIN_SCORE_TO_ALERT = 7   # Claude score out of 10

# ─────────────────────────────────────────────────────────────────────────────
#  SOURCES
# ─────────────────────────────────────────────────────────────────────────────

SOURCES = [
    {
        "name": "REI",
        "url":  "https://www.rei.com/c/hardtail-mountain-bikes?ir=category%3Ahardtail-mountain-bikes&r=q%3A27.5",
        "type": "rei",
    },
    {
        "name": "Jenson USA",
        "url":  "https://www.jensonusa.com/Bikes/Mountain-Bikes/Hardtail--Cross-Country?sortby=1&inStockOnly=true",
        "type": "jenson",
    },
    {
        "name": "Trek",
        "url":  "https://www.trekbikes.com/us/en_US/bikes/mountain-bikes/hardtail-mountain-bikes/",
        "type": "trek",
    },
    {
        "name": "Giant",
        "url":  "https://www.giant-bicycles.com/us/bikes/mountain/hardtail",
        "type": "giant",
    },
    {
        "name": "Specialized",
        "url":  "https://www.specialized.com/us/en/shop/bikes/mountain-bikes/hardtail-mountain-bikes",
        "type": "specialized",
    },
    {
        "name": "Marin",
        "url":  "https://www.marinbikes.com/bikes/mountain/hardtail",
        "type": "marin",
    },
    {
        "name": "The Pro's Closet",
        "url":  "https://www.theproscloset.com/collections/hardtail-mountain-bikes?sort_by=price-ascending&filter.p.m.bike.wheel_size=27.5%22",
        "type": "pros_closet",
    },
]

# ─────────────────────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────────────────────

def log(msg: str):
    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
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
#  CLAUDE EVALUATION
# ─────────────────────────────────────────────────────────────────────────────

CRITERIA_PROMPT = """You are a bike expert evaluating mountain bike listings for a teenage rider.

RIDER: 5'5", beginner-intermediate, neighborhood/bike paths/light trails.

REQUIRED (hard rules — reject if missing):
- Frame size: Small (S) — reject XS, Medium, Large, or larger
- Wheel size: 27.5" (also written as 27.5)
- Hardtail (front suspension only — no full suspension)
- Disc brakes (hydraulic strongly preferred, mechanical acceptable)
- Aluminum frame
- Reputable brand: Trek, Giant, Specialized, Marin, Kona, Cannondale only

PREFERRED (boosts score):
- 1x drivetrain (1x8 minimum, 1x9/10/11/12 better)
- Hydraulic disc brakes (Shimano MT200+ or equivalent)
- Shimano Deore, SLX, XT, CUES, or equivalent
- Modern geometry (short chainstays, slack head angle)
- Lockout fork

REJECT OUTRIGHT (score 0, do not alert):
- 3x drivetrains (e.g., 3x7, 3x8, 3x21 speed)
- Rim brakes
- Unknown/department store brands
- XS, Medium, or larger frames
- 26" wheels
- Full suspension at this price point (likely low quality)

TARGET MODELS — Tier 1 (best value, highest scores):
- Marin Bobcat Trail 5, Giant Talon 2, Trek Marlin 5 Gen 3, Specialized Rockhopper

ACCEPTABLE — Tier 2 (good if priced right):
- Giant Talon 3/4, Trek Marlin 4, Marin Bobcat Trail 4, Kona equivalents

BUDGET & VALUE:
- Ideal: $500–$700
- Acceptable: up to $800 if spec justifies it
- Stretch: $800–$900 ONLY if genuinely upgrade-tier spec
- Entry bikes (~$600–700 MSRP): must be priced below ~$650 to alert
- Mid-tier (~$900–1100 MSRP): good deal at ≤ $800
- Never alert for overpriced entry-level bikes

SIZE NOTE: If size is unclear or not mentioned, assume it may not be Small — score lower."""


def evaluate_listing(listing: dict) -> dict:
    """
    Ask Claude Haiku to evaluate a bike listing.
    Returns dict with score (1-10), verdict, reason, and reject flag.
    """
    if not ANTHROPIC_KEY:
        # Fallback: basic rule-based scoring if no API key
        return _rule_based_score(listing)

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    prompt = f"""{CRITERIA_PROMPT}

---
LISTING TO EVALUATE:
Source: {listing.get('source', 'unknown')}
Model/Title: {listing.get('title', 'unknown')}
Price: ${listing.get('price', 'unknown')}
Original MSRP: {f"${listing['msrp']}" if listing.get('msrp') else 'not listed'}
Size(s) available: {listing.get('size', 'unknown')}
Specs/Description: {listing.get('specs', 'not provided')}
URL: {listing.get('url', '')}
---

Evaluate this listing strictly against the criteria above.
Respond with ONLY valid JSON in this exact format:
{{
  "score": <integer 1-10>,
  "verdict": "<good_deal|fair|skip|reject>",
  "reason": "<one clear sentence explaining your verdict>",
  "reject": <true|false>,
  "size_confirmed": <true|false>
}}

Be strict. Score 7+ only for genuinely good deals. Score 0 and reject=true for hard rejections."""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        # Extract JSON even if there's surrounding text
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        return {"score": 0, "verdict": "skip", "reason": "Could not parse evaluation", "reject": False, "size_confirmed": False}
    except Exception as e:
        log(f"  Claude eval error: {e}")
        return _rule_based_score(listing)


def _rule_based_score(listing: dict) -> dict:
    """Simple fallback scoring when Claude API is unavailable."""
    title  = (listing.get('title', '') + ' ' + listing.get('specs', '')).lower()
    price  = listing.get('price', 9999)
    reject = False
    reason = ""

    # Hard rejects
    if any(x in title for x in ['3x', 'rim brake', 'v-brake', 'xs ', ' xs']):
        return {"score": 0, "verdict": "reject", "reason": "Hard reject: 3x drivetrain or rim brakes", "reject": True, "size_confirmed": False}

    score = 5

    # Price scoring
    if price <= 650:
        score += 2
    elif price <= 750:
        score += 1
    elif price > 900:
        score -= 2

    # Tier 1 models
    if any(m in title for m in ['bobcat trail 5', 'marlin 5', 'talon 2', 'rockhopper']):
        score += 2
    elif any(m in title for m in ['talon 3', 'talon 4', 'marlin 4', 'bobcat trail 4']):
        score += 1

    # Good components
    if any(x in title for x in ['deore', 'slx', 'xt ', 'cues', 'hydraulic']):
        score += 1
    if '1x' in title or 'single' in title:
        score += 1

    score = max(0, min(10, score))
    verdict = "good_deal" if score >= 7 else "fair" if score >= 5 else "skip"
    return {"score": score, "verdict": verdict, "reason": "Rule-based evaluation (Claude unavailable)", "reject": reject, "size_confirmed": False}

# ─────────────────────────────────────────────────────────────────────────────
#  SCRAPERS
# ─────────────────────────────────────────────────────────────────────────────

def _extract_price(text: str) -> float | None:
    """Pull the first dollar amount from a string."""
    m = re.search(r'\$\s*([\d,]+(?:\.\d{2})?)', text)
    if m:
        return float(m.group(1).replace(',', ''))
    return None

def _extract_msrp(text: str) -> float | None:
    """Pull MSRP / was-price from a string."""
    m = re.search(r'(?:msrp|was|reg(?:ular)?|orig(?:inal)?|compare)[^\d$]*\$\s*([\d,]+(?:\.\d{2})?)', text, re.IGNORECASE)
    if m:
        return float(m.group(1).replace(',', ''))
    return None

def _quick_price_filter(price: float | None) -> bool:
    """Pre-filter: skip if clearly out of budget."""
    if price is None:
        return True  # keep — let Claude decide
    return price <= 950


def scrape_rei(page, url: str) -> list[dict]:
    listings = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        cards = page.query_selector_all('[data-ui="product-card"], .VcGDsKAw, [class*="product-card"]')
        if not cards:
            # Fallback: try generic product containers
            cards = page.query_selector_all('li[class*="grid"], div[class*="ProductCard"]')

        for card in cards:
            try:
                text = card.inner_text()
                link_el = card.query_selector('a')
                href = link_el.get_attribute('href') if link_el else ''
                if href and not href.startswith('http'):
                    href = f"https://www.rei.com{href}"

                price = _extract_price(text)
                if not _quick_price_filter(price):
                    continue

                listings.append({
                    "source": "REI",
                    "title":  text.split('\n')[0].strip()[:120],
                    "price":  price,
                    "msrp":   _extract_msrp(text),
                    "size":   _extract_size(text),
                    "specs":  text[:500],
                    "url":    href,
                })
            except Exception:
                continue
    except Exception as e:
        log(f"  REI scrape error: {e}")
    return listings


def scrape_jenson(page, url: str) -> list[dict]:
    listings = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        cards = page.query_selector_all('.product-item, [class*="product-card"], [class*="ProductItem"]')
        if not cards:
            cards = page.query_selector_all('div[data-product-id], li.product')

        for card in cards:
            try:
                text = card.inner_text()
                link_el = card.query_selector('a')
                href = link_el.get_attribute('href') if link_el else ''
                if href and not href.startswith('http'):
                    href = f"https://www.jensonusa.com{href}"

                price = _extract_price(text)
                if not _quick_price_filter(price):
                    continue

                listings.append({
                    "source": "Jenson USA",
                    "title":  text.split('\n')[0].strip()[:120],
                    "price":  price,
                    "msrp":   _extract_msrp(text),
                    "size":   _extract_size(text),
                    "specs":  text[:500],
                    "url":    href,
                })
            except Exception:
                continue
    except Exception as e:
        log(f"  Jenson USA scrape error: {e}")
    return listings


def scrape_trek(page, url: str) -> list[dict]:
    listings = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        cards = page.query_selector_all('.product-card, [class*="ProductCard"], [class*="product-tile"]')
        if not cards:
            cards = page.query_selector_all('article, .grid-item')

        for card in cards:
            try:
                text = card.inner_text()
                link_el = card.query_selector('a')
                href = link_el.get_attribute('href') if link_el else ''
                if href and not href.startswith('http'):
                    href = f"https://www.trekbikes.com{href}"

                price = _extract_price(text)
                if not _quick_price_filter(price):
                    continue

                listings.append({
                    "source": "Trek",
                    "title":  text.split('\n')[0].strip()[:120],
                    "price":  price,
                    "msrp":   _extract_msrp(text),
                    "size":   _extract_size(text),
                    "specs":  text[:500],
                    "url":    href,
                })
            except Exception:
                continue
    except Exception as e:
        log(f"  Trek scrape error: {e}")
    return listings


def scrape_giant(page, url: str) -> list[dict]:
    listings = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        cards = page.query_selector_all('.product-item, [class*="product-card"], [class*="ProductCard"], .bike-card')
        if not cards:
            cards = page.query_selector_all('li[class*="product"], div[class*="bike"]')

        for card in cards:
            try:
                text = card.inner_text()
                link_el = card.query_selector('a')
                href = link_el.get_attribute('href') if link_el else ''
                if href and not href.startswith('http'):
                    href = f"https://www.giant-bicycles.com{href}"

                price = _extract_price(text)
                if not _quick_price_filter(price):
                    continue

                listings.append({
                    "source": "Giant",
                    "title":  text.split('\n')[0].strip()[:120],
                    "price":  price,
                    "msrp":   _extract_msrp(text),
                    "size":   _extract_size(text),
                    "specs":  text[:500],
                    "url":    href,
                })
            except Exception:
                continue
    except Exception as e:
        log(f"  Giant scrape error: {e}")
    return listings


def scrape_specialized(page, url: str) -> list[dict]:
    listings = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(4000)  # Specialized is JS-heavy

        cards = page.query_selector_all('[class*="ProductCard"], [class*="product-card"], [data-testid*="product"]')
        if not cards:
            cards = page.query_selector_all('li[class*="grid"], article')

        for card in cards:
            try:
                text = card.inner_text()
                link_el = card.query_selector('a')
                href = link_el.get_attribute('href') if link_el else ''
                if href and not href.startswith('http'):
                    href = f"https://www.specialized.com{href}"

                price = _extract_price(text)
                if not _quick_price_filter(price):
                    continue

                listings.append({
                    "source": "Specialized",
                    "title":  text.split('\n')[0].strip()[:120],
                    "price":  price,
                    "msrp":   _extract_msrp(text),
                    "size":   _extract_size(text),
                    "specs":  text[:500],
                    "url":    href,
                })
            except Exception:
                continue
    except Exception as e:
        log(f"  Specialized scrape error: {e}")
    return listings


def scrape_marin(page, url: str) -> list[dict]:
    listings = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        cards = page.query_selector_all('.product-card, [class*="product"], [class*="bike-card"], article')

        for card in cards:
            try:
                text = card.inner_text()
                link_el = card.query_selector('a')
                href = link_el.get_attribute('href') if link_el else ''
                if href and not href.startswith('http'):
                    href = f"https://www.marinbikes.com{href}"

                price = _extract_price(text)
                if not _quick_price_filter(price):
                    continue

                listings.append({
                    "source": "Marin",
                    "title":  text.split('\n')[0].strip()[:120],
                    "price":  price,
                    "msrp":   _extract_msrp(text),
                    "size":   _extract_size(text),
                    "specs":  text[:500],
                    "url":    href,
                })
            except Exception:
                continue
    except Exception as e:
        log(f"  Marin scrape error: {e}")
    return listings


def scrape_pros_closet(page, url: str) -> list[dict]:
    listings = []
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        cards = page.query_selector_all('.product-item, [class*="ProductCard"], [class*="product-card"], .grid__item')

        for card in cards:
            try:
                text = card.inner_text()
                link_el = card.query_selector('a')
                href = link_el.get_attribute('href') if link_el else ''
                if href and not href.startswith('http'):
                    href = f"https://www.theproscloset.com{href}"

                price = _extract_price(text)
                if not _quick_price_filter(price):
                    continue

                listings.append({
                    "source": "The Pro's Closet",
                    "title":  text.split('\n')[0].strip()[:120],
                    "price":  price,
                    "msrp":   _extract_msrp(text),
                    "size":   _extract_size(text),
                    "specs":  text[:500],
                    "url":    href,
                })
            except Exception:
                continue
    except Exception as e:
        log(f"  Pro's Closet scrape error: {e}")
    return listings


SCRAPERS = {
    "rei":         scrape_rei,
    "jenson":      scrape_jenson,
    "trek":        scrape_trek,
    "giant":       scrape_giant,
    "specialized": scrape_specialized,
    "marin":       scrape_marin,
    "pros_closet": scrape_pros_closet,
}

# ─────────────────────────────────────────────────────────────────────────────
#  SIZE EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────

def _extract_size(text: str) -> str:
    """Try to extract frame size from listing text."""
    # Look for explicit size mentions
    m = re.search(
        r'\b(x-?small|xsmall|xs|small|sm|\bS\b|medium|med|\bM\b|large|\bL\b|x-?large|xlarge|xl)\b',
        text, re.IGNORECASE
    )
    if m:
        raw = m.group(1).upper()
        mapping = {
            'XSMALL': 'XS', 'X-SMALL': 'XS', 'XS': 'XS',
            'SMALL': 'Small', 'SM': 'Small', 'S': 'Small',
            'MEDIUM': 'Medium', 'MED': 'Medium', 'M': 'Medium',
            'LARGE': 'Large', 'L': 'Large',
            'XLARGE': 'XL', 'X-LARGE': 'XL', 'XL': 'XL',
        }
        return mapping.get(raw, raw)
    return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
#  EMAIL
# ─────────────────────────────────────────────────────────────────────────────

def send_alert(deals: list[dict]):
    if not EMAIL_PASSWORD:
        log("  No EMAIL_APP_PASSWORD — skipping email.")
        return

    deal_rows = ""
    for d in deals:
        listing = d["listing"]
        evaluation = d["evaluation"]
        score_color = "#16a34a" if evaluation["score"] >= 8 else "#d97706"
        msrp_text = f' <span style="color:#9ca3af;text-decoration:line-through">${listing["msrp"]:.0f} MSRP</span>' if listing.get("msrp") else ""

        deal_rows += f"""
        <div style="background:#fff;border:1px solid #e5e7eb;border-left:4px solid {score_color};
                    border-radius:8px;padding:20px;margin-bottom:16px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px;">
            <div>
              <div style="font-size:16px;font-weight:600;color:#111827;">{listing['title']}</div>
              <div style="font-size:12px;color:#6b7280;margin-top:2px;">{listing['source']}</div>
            </div>
            <div style="text-align:right;flex-shrink:0;margin-left:16px;">
              <div style="font-size:20px;font-weight:700;color:#111827;">${listing['price']:.0f}{msrp_text}</div>
              <div style="font-size:12px;font-weight:600;color:{score_color};">
                ★ {evaluation['score']}/10 — {evaluation['verdict'].replace('_',' ').title()}
              </div>
            </div>
          </div>
          <div style="font-size:13px;color:#374151;margin-bottom:12px;">
            <strong>Why it's a good deal:</strong> {evaluation['reason']}
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
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:600px;margin:32px auto;padding:0 16px;">

    <div style="background:linear-gradient(135deg,#4f46e5,#7c3aed);border-radius:12px;
                padding:28px;margin-bottom:24px;text-align:center;">
      <div style="font-size:28px;margin-bottom:6px;">🚲</div>
      <h1 style="color:#fff;font-size:22px;font-weight:700;margin:0 0 6px;">
        Bike Deal Alert
      </h1>
      <p style="color:rgba(255,255,255,0.8);font-size:14px;margin:0;">
        {len(deals)} new deal{'s' if len(deals) != 1 else ''} found matching your criteria
      </p>
    </div>

    {deal_rows}

    <div style="text-align:center;font-size:12px;color:#9ca3af;padding:16px 0 32px;">
      Bike Deal Finder · Checked {datetime.now().strftime('%b %d at %I:%M %p')}
    </div>
  </div>
</body>
</html>"""

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
    log("═══ Bike Deal Finder — run started ═══")

    state = load_state()
    seen_urls = set(state.get("seen_urls", []))
    new_deals  = []

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

        for source in SOURCES:
            name = source["name"]
            log(f"Checking: {name}")

            try:
                page = ctx.new_page()
                scraper = SCRAPERS[source["type"]]
                listings = scraper(page, source["url"])
                page.close()
            except Exception as e:
                log(f"  ERROR on {name}: {e}")
                continue

            log(f"  {len(listings)} listing(s) found — evaluating...")

            for listing in listings:
                # Skip already-seen URLs
                url = listing.get("url", "")
                if url and url in seen_urls:
                    continue

                # Skip if no meaningful title
                if not listing.get("title") or len(listing["title"]) < 5:
                    continue

                # Evaluate with Claude
                try:
                    evaluation = evaluate_listing(listing)
                except Exception as e:
                    log(f"    Eval error for '{listing.get('title', '?')}': {e}")
                    continue

                score   = evaluation.get("score", 0)
                verdict = evaluation.get("verdict", "skip")
                reject  = evaluation.get("reject", False)

                log(f"    [{score}/10] {listing.get('title', '?')[:60]} — {verdict}")

                if url:
                    seen_urls.add(url)

                if not reject and score >= MIN_SCORE_TO_ALERT:
                    new_deals.append({"listing": listing, "evaluation": evaluation})
                    log(f"    ★ DEAL FOUND: {listing.get('title', '?')}")

            time.sleep(2)  # Be polite between sources

        browser.close()

    # Save state
    state["seen_urls"]   = list(seen_urls)[-2000:]  # Cap to prevent unbounded growth
    state["last_run"]    = datetime.now(timezone.utc).isoformat()
    state["deals_found"] = state.get("deals_found", []) + [
        {
            "title":   d["listing"].get("title"),
            "price":   d["listing"].get("price"),
            "source":  d["listing"].get("source"),
            "score":   d["evaluation"].get("score"),
            "reason":  d["evaluation"].get("reason"),
            "url":     d["listing"].get("url"),
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


if __name__ == "__main__":
    main()
