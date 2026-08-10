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
# Hard safety gate: never send a listing older than this at run time.
MAX_LISTING_AGE_MINUTES = 35

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
            "https://www.myhome.ge/udzravi-qoneba/qiravdeba/bina/"
            "tbilisi/vake/?deal_types=2&real_estate_types=1%2C2%2C3"
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
    if site == "MyHome.ge":
        match = re.search(
            r"/(?:udzravi-qoneba|real-estate)/(\d+)(?:[/?#]|$)",
            url,
            re.IGNORECASE,
        )
    else:
        match = re.search(
            r"/real-estate/(?!l/)[^?#]*-(\d+)(?:[/?#]|$)",
            url,
        )

    return match.group(1) if match else None


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
                    await page.goto(
                        page_url(source["url"], page_number),
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
                    print(f"{source['key']} page {page_number}: {len(page_listing_ids)} listing IDs detected")

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
    return list(found.values()), errors


def default_state() -> dict[str, Any]:
    return {
        "initialized": False,
        "seen": {source["key"]: [] for source in SOURCES},
        "max_ids": {source["key"]: 0 for source in SOURCES},
        "heartbeat_week": "",
        "last_successful_scan_at": "",
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


def send_telegram(text: str) -> bool:
    return telegram_api_post(
        "sendMessage",
        {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": "true"},
    )


def extract_exact_listing_location(page_html: str, url: str = "", heading_text: str = "") -> str:
    canonical_locations = [
        ("ნუცუბიძის ფერდობი", ["ნუცუბიძის ფერდობ"]),
        ("ლისის მიმდებარედ", ["ლისის მიმდებარ"]),
        ("თბილისის ზღვა", ["თბილისის ზღვა"]),
        ("დიდი დიღომი", ["დიდი დიღომ"]),
        ("დიღმის მასივი", ["დიღმის მასივ", "დიღომის მასივ"]),
        ("საბურთალო", ["საბურთალ"]),
        ("ვაკე", ["ვაკე", "ვაკეში"]),
        ("ვერა", ["ვერა", "ვერაზე"]),
        ("მთაწმინდა", ["მთაწმინდ"]),
        ("სოლოლაკი", ["სოლოლაკ"]),
        ("ჩუღურეთი", ["ჩუღურეთ"]),
        ("დიდუბე", ["დიდუბ"]),
        ("ნაძალადევი", ["ნაძალადევ"]),
        ("გლდანი", ["გლდან"]),
        ("მუხიანი", ["მუხიან"]),
        ("ვარკეთილი", ["ვარკეთილ"]),
        ("ისანი", ["ისან"]),
        ("სამგორი", ["სამგორ"]),
        ("ავლაბარი", ["ავლაბარ"]),
        ("ორთაჭალა", ["ორთაჭალ"]),
        ("კრწანისი", ["კრწანის"]),
        ("ვაზისუბანი", ["ვაზისუბან"]),
        ("ლილო", ["ლილო"]),
        ("ოქროყანა", ["ოქროყან"]),
        ("წყნეთი", ["წყნეთ"]),
        ("კოჯორი", ["კოჯორ"]),
        ("ტაბახმელა", ["ტაბახმელ"]),
        ("დიღომი", ["დიღომ"]),
    ]

    def match_canonical(value: str) -> str:
        value = normalize_text(value).lower()
        for canonical, stems in canonical_locations:
            for stem in stems:
                if stem.lower() in value:
                    return canonical
        return ""

    heading_location = match_canonical(heading_text)
    if heading_location:
        return heading_location

    decoded_html = html.unescape(page_html)
    structured_patterns = [
        r'"districtName"\s*:\s*"([^"]+)"',
        r'"district_name"\s*:\s*"([^"]+)"',
        r'"district"\s*:\s*\{[^{}]{0,800}?"name"\s*:\s*"([^"]+)"',
        r'"urbanName"\s*:\s*"([^"]+)"',
        r'"urban_name"\s*:\s*"([^"]+)"',
        r'"locationName"\s*:\s*"([^"]+)"',
    ]
    structured_candidates = []
    for pattern in structured_patterns:
        for match in re.finditer(pattern, decoded_html, re.IGNORECASE | re.DOTALL):
            location = match_canonical(match.group(1))
            if location:
                structured_candidates.append(location)
    structured_candidates = list(dict.fromkeys(structured_candidates))
    if len(structured_candidates) == 1:
        return structured_candidates[0]

    lowered_url = urllib.parse.unquote(url).lower()
    slug_locations = [
        ("nutsubidzis-ferdob", "ნუცუბიძის ფერდობი"),
        ("didi-dighom", "დიდი დიღომი"),
        ("dighmis-masiv", "დიღმის მასივი"),
        ("dighomis-masiv", "დიღმის მასივი"),
        ("saburtalo", "საბურთალო"),
        ("vake", "ვაკე"),
        ("vera", "ვერა"),
        ("mtatsminda", "მთაწმინდა"),
        ("sololaki", "სოლოლაკი"),
        ("chughureti", "ჩუღურეთი"),
        ("didube", "დიდუბე"),
        ("nadzaladevi", "ნაძალადევი"),
        ("gldani", "გლდანი"),
        ("mukhiani", "მუხიანი"),
        ("mughiani", "მუხიანი"),
        ("varketili", "ვარკეთილი"),
        ("isani", "ისანი"),
        ("samgori", "სამგორი"),
        ("avlabari", "ავლაბარი"),
        ("ortachala", "ორთაჭალა"),
        ("krtsanisi", "კრწანისი"),
        ("vazisubani", "ვაზისუბანი"),
        ("lilo", "ლილო"),
        ("oqroqana", "ოქროყანა"),
        ("tsqneti", "წყნეთი"),
        ("tskneti", "წყნეთი"),
        ("kojori", "კოჯორი"),
        ("tabakhmela", "ტაბახმელა"),
        ("dighomi", "დიღომი"),
    ]
    for slug, location in slug_locations:
        if slug in lowered_url:
            return location
    return "თბილისი"


def get_listing_details(url: str) -> dict[str, str]:
    details = {"image_url": "", "location": ""}
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "ka,en-US;q=0.9,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            page_html = response.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        print(f"Could not open listing page: {url}: {exc}")
        return details

    image_patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
    ]
    for pattern in image_patterns:
        match = re.search(pattern, page_html, re.IGNORECASE)
        if match:
            image_url = html.unescape(match.group(1)).strip()
            if image_url.startswith("//"):
                image_url = "https:" + image_url
            elif image_url.startswith("/"):
                image_url = urllib.parse.urljoin(url, image_url)
            if image_url.startswith("http"):
                details["image_url"] = image_url
                break
    details["location"] = extract_exact_listing_location(page_html, url)
    return details


def parse_iso_datetime(value: str) -> datetime | None:
    value = normalize_text(value)
    if not value:
        return None
    value = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=GEORGIA_TZ)
    return parsed.astimezone(timezone.utc)


def parse_myhome_posted_at(rendered_text: str, page_html: str, now_local: datetime) -> datetime | None:
    decoded_html = html.unescape(page_html)
    structured_patterns = [
        r'"createdAt"\s*:\s*"([^"]+)"', r'"created_at"\s*:\s*"([^"]+)"',
        r'"createDate"\s*:\s*"([^"]+)"', r'"publishDate"\s*:\s*"([^"]+)"',
        r'"publishedAt"\s*:\s*"([^"]+)"', r'"published_at"\s*:\s*"([^"]+)"',
    ]
    for pattern in structured_patterns:
        structured_match = re.search(pattern, decoded_html, re.IGNORECASE)
        if structured_match:
            parsed = parse_iso_datetime(structured_match.group(1))
            if parsed is not None:
                return parsed

    numeric_patterns = [
        r'"createdAt"\s*:\s*(\d{10,13})', r'"created_at"\s*:\s*(\d{10,13})',
        r'"createDate"\s*:\s*(\d{10,13})', r'"publishDate"\s*:\s*(\d{10,13})',
        r'"publishedAt"\s*:\s*(\d{10,13})', r'"published_at"\s*:\s*(\d{10,13})',
    ]
    for pattern in numeric_patterns:
        numeric_match = re.search(pattern, decoded_html, re.IGNORECASE)
        if numeric_match:
            raw = int(numeric_match.group(1))
            if raw > 10_000_000_000:
                raw = raw / 1000
            try:
                return datetime.fromtimestamp(raw, tz=timezone.utc)
            except (OSError, OverflowError, ValueError):
                pass

    rendered = normalize_text(rendered_text)
    today_match = re.search(r"(?:^|\s)დღეს\s+(\d{1,2}):(\d{2})(?:-?ზე)?(?:\s|$)", rendered, re.IGNORECASE)
    if today_match:
        hour = int(today_match.group(1)); minute = int(today_match.group(2))
        if hour == 0 and minute == 0:
            print("Publication time is visible as today 00:00 without a structured timestamp; treating as unverified")
            return None
        return now_local.replace(hour=hour, minute=minute, second=0, microsecond=0).astimezone(timezone.utc)

    yesterday_match = re.search(r"(?:^|\s)გუშინ\s+(\d{1,2}):(\d{2})(?:-?ზე)?(?:\s|$)", rendered, re.IGNORECASE)
    if yesterday_match:
        yesterday = now_local - timedelta(days=1)
        return yesterday.replace(hour=int(yesterday_match.group(1)), minute=int(yesterday_match.group(2)), second=0, microsecond=0).astimezone(timezone.utc)

    months = {
        "იანვარი": 1, "იანვარს": 1, "თებერვალი": 2, "თებერვალს": 2,
        "მარტი": 3, "მარტს": 3, "აპრილი": 4, "აპრილს": 4,
        "მაისი": 5, "მაისს": 5, "ივნისი": 6, "ივნისს": 6,
        "ივლისი": 7, "ივლისს": 7, "აგვისტო": 8, "აგვისტოს": 8,
        "სექტემბერი": 9, "სექტემბერს": 9, "ოქტომბერი": 10, "ოქტომბერს": 10,
        "ნოემბერი": 11, "ნოემბერს": 11, "დეკემბერი": 12, "დეკემბერს": 12,
    }
    month_pattern = "|".join(sorted((re.escape(month) for month in months), key=len, reverse=True))
    explicit_match = re.search(
        rf"(?:^|\s)(\d{{1,2}})\s+({month_pattern})(?:\s+(\d{{4}}))?(?:\s+(\d{{1,2}}):(\d{{2}})(?:-?ზე)?)?",
        rendered,
        re.IGNORECASE,
    )
    if explicit_match:
        year = int(explicit_match.group(3)) if explicit_match.group(3) else now_local.year
        try:
            local_dt = datetime(
                year, months[explicit_match.group(2).lower()], int(explicit_match.group(1)),
                int(explicit_match.group(4) or "0"), int(explicit_match.group(5) or "0"), tzinfo=GEORGIA_TZ,
            )
            return local_dt.astimezone(timezone.utc)
        except ValueError:
            pass
    return None


async def enrich_unseen_items(items: list[dict[str, Any]]) -> None:
    if not items:
        return
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            locale="ka-GE",
            viewport={"width": 1440, "height": 1200},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        )
        await context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        for item in items:
            page = await context.new_page()
            try:
                await page.goto(item["url"], wait_until="domcontentloaded", timeout=90_000)
                await page.wait_for_timeout(2_500)
                rendered_text = normalize_text(await page.locator("body").inner_text())
                page_html = await page.content()
                now_local = datetime.now(GEORGIA_TZ)
                posted_at = parse_myhome_posted_at(rendered_text, page_html, now_local)
                item["posted_at_utc"] = posted_at.isoformat() if posted_at is not None else ""

                heading_parts: list[str] = []
                for selector in ("h1", "h2", "h3"):
                    locator = page.locator(selector)
                    try:
                        count = min(await locator.count(), 5)
                    except Exception:
                        count = 0
                    for index in range(count):
                        try:
                            value = normalize_text(await locator.nth(index).inner_text())
                        except Exception:
                            continue
                        if not value:
                            continue
                        if selector == "h1":
                            heading_parts.append(value)
                            continue
                        if "ქირავდება" in value or "იყიდება" in value:
                            heading_parts.append(value)

                listing_heading = " ".join(heading_parts)
                item["location"] = extract_exact_listing_location(page_html, item["url"], listing_heading)

                og_image = await page.locator('meta[property="og:image"]').get_attribute("content")
                if not og_image:
                    og_image = await page.locator('meta[name="twitter:image"]').get_attribute("content")
                if og_image:
                    og_image = html.unescape(og_image).strip()
                    if og_image.startswith("//"):
                        og_image = "https:" + og_image
                    elif og_image.startswith("/"):
                        og_image = urllib.parse.urljoin(item["url"], og_image)
                    if og_image.startswith("http"):
                        item["detail_image_url"] = og_image
                print(f"Detail {item['listing_id']}: posted_at={item.get('posted_at_utc') or 'UNKNOWN'}, location={item.get('location')}")
            except Exception as exc:
                item["posted_at_utc"] = ""
                print(f"Detail check failed for {item['listing_id']}: {type(exc).__name__}: {exc}")
            finally:
                await page.close()
        await context.close()
        await browser.close()


def is_fresh_listing(item: dict[str, Any], previous_scan_at: datetime, run_started_at: datetime) -> bool:
    posted_raw = item.get("posted_at_utc", "")
    if not posted_raw:
        print(f"SKIP {item['listing_id']}: publication time could not be verified")
        return False
    posted_at = parse_iso_datetime(posted_raw)
    if posted_at is None:
        print(f"SKIP {item['listing_id']}: invalid publication timestamp {posted_raw!r}")
        return False
    scan_lower_bound = previous_scan_at - timedelta(minutes=FRESHNESS_GRACE_MINUTES)
    absolute_lower_bound = run_started_at - timedelta(minutes=MAX_LISTING_AGE_MINUTES)
    lower_bound = max(scan_lower_bound, absolute_lower_bound)
    upper_bound = run_started_at + timedelta(minutes=FRESHNESS_GRACE_MINUTES)
    fresh = lower_bound <= posted_at <= upper_bound
    if not fresh:
        age_minutes = (run_started_at - posted_at).total_seconds() / 60
        print(f"SKIP {item['listing_id']}: not fresh; posted={posted_at.isoformat()}, age={age_minutes:.1f}min, allowed={lower_bound.isoformat()}..{upper_bound.isoformat()}")
    return fresh


def send_listing_to_telegram(item: dict[str, Any]) -> bool:
    item = dict(item)
    image_url = (item.get("detail_image_url") or "").strip()
    if not image_url:
        details = get_listing_details(item["url"])
        if details["location"] and item.get("location") == "თბილისი":
            item["location"] = details["location"]
        image_url = details["image_url"]
    caption = format_listing(item)
    if image_url:
        if telegram_api_post(
            "sendPhoto",
            {"chat_id": CHAT_ID, "photo": image_url, "caption": caption, "parse_mode": "HTML"},
        ):
            return True
        print(f"Photo delivery failed for {item['listing_id']}; trying text-only fallback")
    return send_telegram(caption)


def format_listing(item: dict[str, Any]) -> str:
    rooms = item["rooms"] if item["rooms"] is not None else "—"
    safe_url = html.escape(item["url"], quote=True)
    return (
        "🏠 <b>ახალი განცხადება</b>\n\n"
        f"🌐 <b>საიტი:</b> {html.escape(item['site'])}\n"
        "🔑 <b>გარიგება:</b> ქირავდება\n"
        f"📍 <b>უბანი:</b> {html.escape(item['location'])}\n"
        f"🚪 <b>ოთახები:</b> {rooms}\n"
        f"📐 <b>ფართობი:</b> {html.escape(item['area'])}\n"
        f"💰 <b>ფასი:</b> {html.escape(item['price'])}\n\n"
        f"🔗 <a href=\"{safe_url}\">განცხადების გახსნა</a>"
    )


async def main() -> int:
    if not TOKEN or not CHAT_ID:
        print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID", file=sys.stderr)
        return 2
    run_started_at = datetime.now(timezone.utc)
    state = load_state()
    listings, errors = await scrape_all_sources()
    if errors:
        print("Errors:")
        for error in errors:
            print(f"- {error}")
    if not listings and errors:
        return 1

    initialized = bool(state["initialized"])
    previous_scan_at = parse_iso_datetime(normalize_text(state.get("last_successful_scan_at", "")))
    unseen_items: list[dict[str, Any]] = []
    for item in listings:
        key = item["source_key"]
        listing_id = item["listing_id"]
        seen = state["seen"][key]
        if listing_id not in seen:
            unseen_items.append(item)
            seen.append(listing_id)
            state["seen"][key] = seen[-20000:]
        state["max_ids"][key] = max(int(state["max_ids"][key]), int(listing_id))

    new_items: list[dict[str, Any]] = []
    if not initialized:
        state["initialized"] = True
        send_telegram(
            "✅ მონიტორინგი ჩართულია.\n\n"
            "ვაკონტროლებ MyHome.ge-ზე თბილისში გასაქირავებელ ბინებს, კერძო სახლებსა და აგარაკებს — 400$-დან, "
            "მხოლოდ ფიზიკური პირების განცხადებებს.\n\n"
            "არსებული განცხადებები შენახულია; შეტყობინებები მოვა მხოლოდ ახალი განცხადებების დამატებისას."
        )
        print(f"Initial baseline: stored {len(unseen_items)} currently visible listings")
    elif previous_scan_at is None:
        print(f"Freshness baseline created. Stored {len(unseen_items)} unseen listings without notifications.")
    else:
        await enrich_unseen_items(unseen_items)
        for item in unseen_items:
            if is_fresh_listing(item, previous_scan_at, run_started_at):
                new_items.append(item)
        new_items.sort(
            key=lambda item: (
                parse_iso_datetime(item.get("posted_at_utc", "")) or datetime.min.replace(tzinfo=timezone.utc),
                int(item["listing_id"]),
            )
        )
        sent_count = 0
        failed_items: list[dict[str, Any]] = []
        for item in new_items:
            if send_listing_to_telegram(item):
                sent_count += 1
            else:
                failed_items.append(item)
                print(f"Telegram delivery failed for {item['listing_id']}; it will be retried on the next run")
        for item in failed_items:
            key = item["source_key"]
            listing_id = item["listing_id"]
            state["seen"][key] = [seen_id for seen_id in state["seen"][key] if seen_id != listing_id]

    if not initialized or previous_scan_at is None:
        sent_count = 0
    state["heartbeat_week"] = run_started_at.strftime("%G-W%V")
    state["last_successful_scan_at"] = run_started_at.isoformat()
    save_state(state)
    print(f"Found {len(listings)} listings; unseen {len(unseen_items)}; verified-new {len(new_items)}; sent {sent_count} notifications")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
