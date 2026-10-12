import asyncio
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


STATE_PATH = Path(__file__).resolve().parent / "state.json"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

_NBG_RATES_CACHE: dict[str, float] | None = None
GEORGIA_TZ = ZoneInfo("Asia/Tbilisi")
FRESHNESS_GRACE_MINUTES = 5
# Normal production safety gate. Keep the established 35-minute behavior.
MAX_LISTING_AGE_MINUTES = 50

# A healthy production scan normally sees ~400+ listings across 22 pages.
# Refuse to advance state when MyHome changes/breaks rendering and the
# scraper suddenly sees an implausibly small result set.
MIN_HEALTHY_LISTINGS = 100

# One-time outage recovery. The last confirmed Telegram delivery was around
# 2026-08-13 13:17 Asia/Tbilisi. A small buffer starts recovery at 13:10.
# Existing seen IDs still prevent duplicates. After one healthy recovery run
# state["url_change_recovery_completed"] becomes True and normal 35-minute
# freshness behavior resumes automatically.
URL_CHANGE_RECOVERY_START_UTC = datetime(
    2026, 8, 13, 9, 10, tzinfo=timezone.utc
)

TELEGRAM_MIN_INTERVAL_SECONDS = 3.2
TELEGRAM_MAX_ATTEMPTS = 6
_LAST_TELEGRAM_SEND_MONOTONIC = 0.0

SOURCES = [
    {
        "key": "myhome_rent",
        "site": "MyHome.ge",
        "deal": "rent",
        "rooms": None,
        "pages": 22,
        "url": (
            "https://www.myhome.ge/en/real-estate/rent/apartment/tbilisi/"
            "?deal_types=2&real_estate_types=1%2C2%2C3"
            "&cities=1&urbans=38%2C39%2C40%2C41%2C42%2C43%2C44%2C45"
            "%2C47%2C101%2C28%2C48%2C106%2C111%2C30%2C46%2C121%2C29"
            "%2C52%2C53%2C54%2C55%2C78%2C117%2C49%2C50%2C51%2C56"
            "%2C58%2C59%2C60%2C2%2C3%2C5%2C6%2C7%2C8%2C9%2C10"
            "%2C11%2C120%2C1%2C4%2C12%2C122%2C23%2C24%2C25%2C27"
            "%2C103%2C26%2C68%2C13%2C14%2C15%2C16%2C17%2C18%2C19"
            "%2C20%2C21%2C22%2C69%2C70%2C102%2C118%2C63%2C64%2C65"
            "%2C66%2C67%2C107%2C61%2C62%2C57&districts=4%2C5%2C1"
            "%2C3%2C2%2C6&currency_id=2&CardView=1&price_from=400"
            "&page=1&owner_type=physical"
        ),
    },
]


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def extract_room_count(text: str, url: str) -> int | None:
    combined = f"{text} {url}"
    for pattern in (
        r"(?<!\d)(\d+)\s*[- ]?room\b",
        r"(?<!\d)(\d+)\s*ოთახ",
        r"(?<!\d)(\d+)\s*комнат",
    ):
        match = re.search(pattern, combined, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def get_nbg_rates() -> dict[str, float]:
    global _NBG_RATES_CACHE

    if _NBG_RATES_CACHE is not None:
        return _NBG_RATES_CACHE

    url = (
        "https://nbg.gov.ge/gw/api/ct/"
        "monetarypolicy/currencies/ka/json/"
    )

    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
        )

        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))

        currencies = payload[0]["currencies"]
        _NBG_RATES_CACHE = {
            item["code"]: float(item["rate"]) / float(item["quantity"])
            for item in currencies
        }

    except Exception as exc:
        print(f"Could not load NBG exchange rates: {exc}")
        _NBG_RATES_CACHE = {}

    return _NBG_RATES_CACHE


def parse_price_number(raw: str) -> float | None:
    cleaned = re.sub(r"[^0-9.,]", "", raw)

    if not cleaned:
        return None

    if re.fullmatch(r"\d{1,3}(?:,\d{3})+", cleaned):
        cleaned = cleaned.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(?:\.\d{3})+", cleaned):
        cleaned = cleaned.replace(".", "")
    else:
        cleaned = cleaned.replace(",", ".")

    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_price(text: str) -> str:
    match = re.search(
        r"(?<!\d)(\d[\d ,.]*?)\s*(₾|\$|€)",
        text,
    )

    if not match:
        return "—"

    amount = parse_price_number(match.group(1))
    currency = match.group(2)

    if amount is None:
        return "—"

    if currency == "$":
        usd = amount
    else:
        rates = get_nbg_rates()
        usd_gel = rates.get("USD")

        if not usd_gel:
            return "—"

        if currency == "₾":
            usd = amount / usd_gel
        elif currency == "€":
            eur_gel = rates.get("EUR")

            if not eur_gel:
                return "—"

            usd = amount * eur_gel / usd_gel
        else:
            return "—"

    rounded = int(round(usd))
    return f"${rounded:,}"


def extract_area(text: str) -> str:
    match = re.search(
        r"(\d+(?:[.,]\d+)?)\s*(?:m²|m2|მ²)",
        text,
        re.IGNORECASE,
    )
    return f"{match.group(1)} m²" if match else "—"


def is_target_location(text: str, url: str) -> bool:
    return True


def extract_location(text: str) -> str:
    locations = [
        "საბურთალო",
        "ვაკე",
        "ლისის მიმდებარედ",
        "ნუცუბიძის ფერდობი",
        "დიდი დიღომი",
        "დიღომი",
        "ვერა",
        "მთაწმინდა",
        "სოლოლაკი",
        "ჩუღურეთი",
        "დიდუბე",
        "ნაძალადევი",
        "გლდანი",
        "მუხიანი",
        "ვარკეთილი",
        "ისანი",
        "სამგორი",
        "ავლაბარი",
        "ორთაჭალა",
        "კრწანისი",
        "ვაზისუბანი",
        "ლილო",
        "თბილისის ზღვა",
        "ოქროყანა",
        "წყნეთი",
        "კოჯორი",
        "ტაბახმელა",
    ]

    lowered = normalize_text(text).lower()

    for location in locations:
        if location.lower() in lowered:
            return location

    return "თბილისი"


def listing_id_from_url(site: str, url: str) -> str | None:
    """Extract a listing ID from both legacy and current MyHome URL formats."""
    if site != "MyHome.ge":
        match = re.search(
            r"/real-estate/(?!l/)[^?#]*-(\d+)(?:[/?#]|$)",
            url,
            re.IGNORECASE,
        )
        return match.group(1) if match else None

    try:
        parts = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(parts.path)
    except Exception:
        path = urllib.parse.unquote(url)

    # Only treat URLs under known MyHome listing roots as listing candidates.
    if not re.search(r"/(?:udzravi-qoneba|real-estate)(?:/|$)", path, re.IGNORECASE):
        return None

    patterns = (
        r"/(?:udzravi-qoneba|real-estate)/(\d{6,10})(?:/|$)",
        r"/(?:udzravi-qoneba|real-estate)/[^/?#]*-(\d{6,10})(?:/|$)",
        r"-(\d{6,10})(?:/)?$",
        r"/(\d{6,10})(?:/)?$",
    )

    for regex in patterns:
        match = re.search(regex, path, re.IGNORECASE)
        if match:
            return match.group(1)

    try:
        query = urllib.parse.parse_qs(parts.query)
    except Exception:
        query = {}

    for key in ("id", "listing_id", "statement_id", "pr_id"):
        values = query.get(key, [])
        for value in values:
            if re.fullmatch(r"\d{6,10}", value or ""):
                return value

    return None


def page_url(url: str, page_number: int) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "page"]
    query.append(("page", str(page_number)))

    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )


async def collect_page_links(page: Any) -> list[dict[str, str]]:
    return await page.evaluate(
        r"""
        () => {
          const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
          const results = [];
          for (const anchor of document.querySelectorAll('a[href]')) {
            const href = anchor.href;
            let text = clean(anchor.innerText || anchor.getAttribute('aria-label') || anchor.title);
            let node = anchor;
            for (let i = 0; i < 6 && node && node.parentElement; i++) {
              node = node.parentElement;
              const candidate = clean(node.innerText);
              const hasMarker = /m²|m2|room|ოთახ|комнат/i.test(candidate);
              if (hasMarker && candidate.length >= 25 && candidate.length <= 1200) {
                text = candidate;
                break;
              }
            }
            let imageUrl = "";
            const imageNode =
              (node && node.querySelector && node.querySelector("img")) ||
              (anchor.querySelector && anchor.querySelector("img"));
            if (imageNode) {
              imageUrl = imageNode.currentSrc || imageNode.src ||
                imageNode.getAttribute("data-src") ||
                imageNode.getAttribute("data-lazy-src") || "";
            }
            results.push({href, text, imageUrl});
          }
          return results;
        }
        """
    )


async def scrape_all_sources():
    found: dict[tuple[str, str], dict[str, Any]] = {}
    errors: list[str] = []
    empty_pages: list[str] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            locale="en-US",
            viewport={"width": 1440, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        for source in SOURCES:
            page_count = int(source.get("pages", 1))
            for page_number in range(1, page_count + 1):
                page = await context.new_page()
                try:
                    print(f"Checking {source['key']} page {page_number}")
                    requested_url = page_url(source["url"], page_number)
                    candidates: list[dict[str, str]] = []
                    page_listing_ids: set[str | None] = set()
                    for attempt in range(1, 4):
                        await page.goto(
                            requested_url,
                            wait_until="domcontentloaded",
                            timeout=90_000,
                        )
                        await page.wait_for_timeout(5_000)
                        for _ in range(3):
                            await page.mouse.wheel(0, 1600)
                            await page.wait_for_timeout(700)

                        candidates = await collect_page_links(page)
                        page_listing_ids = {
                            listing_id_from_url(source["site"], item["href"])
                            for item in candidates
                        }
                        page_listing_ids.discard(None)
                        print(
                            f"{source['key']} page {page_number}: "
                            f"{len(page_listing_ids)} listing IDs detected "
                            f"(attempt {attempt}/3)"
                        )
                        if page_listing_ids:
                            break
                        if attempt < 3:
                            print(
                                f"{source['key']} page {page_number}: "
                                "unexpected empty result; retrying same page"
                            )
                            await page.wait_for_timeout(2_000)

                    if not page_listing_ids:
                        empty_pages.append(f"{source['key']} page {page_number}")
                        continue

                    for candidate in candidates:
                        url = candidate["href"].split("#", 1)[0]
                        listing_id = listing_id_from_url(source["site"], url)
                        if not listing_id:
                            continue
                        text = normalize_text(candidate["text"])
                        if not is_target_location(text, url):
                            continue
                        rooms = extract_room_count(text, url)
                        room_filter = source.get("rooms")
                        if room_filter and rooms not in room_filter:
                            continue
                        key = (source["key"], listing_id)
                        current = found.get(key)
                        if current and len(current["summary"]) >= len(text):
                            continue
                        found[key] = {
                            "source_key": source["key"],
                            "site": source["site"],
                            "deal": source["deal"],
                            "listing_id": listing_id,
                            "url": url,
                            "rooms": rooms,
                            "price": extract_price(text),
                            "area": extract_area(text),
                            "location": extract_location(text),
                            "image_url": candidate.get("imageUrl", ""),
                            "summary": text[:700],
                        }
                except PlaywrightTimeoutError:
                    errors.append(f"{source['key']} page {page_number}: timeout")
                except Exception as exc:
                    errors.append(f"{source['key']} page {page_number}: {type(exc).__name__}: {exc}")
                finally:
                    await page.close()
        await context.close()
        await browser.close()
    return list(found.values()), errors, empty_pages


def default_state() -> dict[str, Any]:
    return {
        "initialized": False,
        "seen": {source["key"]: [] for source in SOURCES},
        "max_ids": {source["key"]: 0 for source in SOURCES},
        "heartbeat_week": "",
        "last_successful_scan_at": "",
        "url_change_recovery_completed": False,
    }


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return default_state()
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default_state()
    template = default_state()
    for key, value in template.items():
        state.setdefault(key, value)
    for source in SOURCES:
        state["seen"].setdefault(source["key"], [])
        state["max_ids"].setdefault(source["key"], 0)
    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def telegram_api_post(method: str, payload: dict[str, str], *, max_attempts: int = TELEGRAM_MAX_ATTEMPTS) -> bool:
    global _LAST_TELEGRAM_SEND_MONOTONIC
    endpoint = f"https://api.telegram.org/bot{TOKEN}/{method}"
    for attempt in range(1, max_attempts + 1):
        elapsed = time.monotonic() - _LAST_TELEGRAM_SEND_MONOTONIC
        delay = TELEGRAM_MIN_INTERVAL_SECONDS - elapsed
        if delay > 0:
            time.sleep(delay)
        request = urllib.request.Request(
            endpoint,
            data=urllib.parse.urlencode(payload).encode("utf-8"),
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                body = response.read().decode("utf-8", errors="replace")
                if response.status != 200:
                    print(f"Telegram {method} returned HTTP {response.status}: {body}")
                    return False
                _LAST_TELEGRAM_SEND_MONOTONIC = time.monotonic()
                return True
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 429:
                retry_after = 5
                try:
                    error_json = json.loads(error_body)
                    retry_after = int(error_json.get("parameters", {}).get("retry_after", retry_after))
                except (json.JSONDecodeError, TypeError, ValueError):
                    header_value = exc.headers.get("Retry-After")
                    if header_value:
                        try:
                            retry_after = int(header_value)
                        except ValueError:
                            pass
                wait_seconds = max(retry_after + 1, int(TELEGRAM_MIN_INTERVAL_SECONDS) + 1)
                print(f"Telegram 429 on {method}; waiting {wait_seconds}s (attempt {attempt}/{max_attempts})")
                time.sleep(wait_seconds)
                continue
            print(f"Telegram {method} HTTP {exc.code}: {error_body}")
            return False
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt >= max_attempts:
                print(f"Telegram {method} failed after {max_attempts} attempts: {exc}")
                return False
            backoff = min(2 ** attempt, 20)
            print(f"Telegram {method} temporary error: {exc}; retrying in {backoff}s")
            time.sleep(backoff)
    return False


def send_telegram(
