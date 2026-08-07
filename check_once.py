import asyncio
import html
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


STATE_PATH = Path(__file__).resolve().parent / "state.json"
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

SOURCES = [
    {
        "key": "myhome_rent",
        "site": "MyHome.ge",
        "deal": "rent",
        "rooms": None,
        "pages": 50,
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


def extract_price(text: str) -> str:
    match = re.search(r"(?<!\d)(\d[\d ,.]*?)\s*(₾|\$|€)", text)
    return normalize_text("".join(match.groups())) if match else "—"


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
    return "თბილისი"


def listing_id_from_url(site: str, url: str) -> str | None:
    if site == "MyHome.ge":
        match = re.search(r"/real-estate/(\d+)(?:/|$)", url)
    else:
        match = re.search(
            r"/real-estate/(?!l/)[^?#]*-(\d+)(?:[/?#]|$)",
            url,
        )

    return match.group(1) if match else None


def page_url(url: str, page_number: int) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(
        parts.query,
        keep_blank_values=True,
    )
    query = [
        (key, value)
        for key, value in query
        if key != "page"
    ]
    query.append(("page", str(page_number)))

    return urllib.parse.urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urllib.parse.urlencode(query),
            parts.fragment,
        )
    )


async def collect_page_links(page: Any) -> list[dict[str, str]]:
    return await page.evaluate(
        r"""
        () => {
          const clean = (value) =>
            (value || '').replace(/\s+/g, ' ').trim();

          const results = [];

          for (const anchor of document.querySelectorAll('a[href]')) {
            const href = anchor.href;

            let text = clean(
              anchor.innerText ||
              anchor.getAttribute('aria-label') ||
              anchor.title
            );

            let node = anchor;

            for (
              let i = 0;
              i < 6 && node && node.parentElement;
              i++
            ) {
              node = node.parentElement;

              const candidate = clean(node.innerText);
              const hasMarker =
                /m²|m2|room|ოთახ|комнат/i.test(candidate);

              if (
                hasMarker &&
                candidate.length >= 25 &&
                candidate.length <= 1200
              ) {
                text = candidate;
                break;
              }
            }

            results.push({href, text});
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
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = await browser.new_context(
            locale="en-US",
            viewport={
                "width": 1440,
                "height": 1200,
            },
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0.0.0 Safari/537.36"
            ),
        )

        await context.add_init_script(
            "Object.defineProperty("
            "navigator, 'webdriver', "
            "{get: () => undefined})"
        )

        for source in SOURCES:
            page_count = int(source.get("pages", 1))

            for page_number in range(1, page_count + 1):
                page = await context.new_page()

                try:
                    print(
                        f"Checking {source['key']} "
                        f"page {page_number}"
                    )

                    await page.goto(
                        page_url(
                            source["url"],
                            page_number,
                        ),
                        wait_until="domcontentloaded",
                        timeout=90_000,
                    )

                    await page.wait_for_timeout(5_000)

                    for _ in range(3):
                        await page.mouse.wheel(0, 1600)
                        await page.wait_for_timeout(700)

                    candidates = await collect_page_links(page)

                    for candidate in candidates:
                        url = candidate["href"].split("#", 1)[0]

                        listing_id = listing_id_from_url(
                            source["site"],
                            url,
                        )

                        if not listing_id:
                            continue

                        text = normalize_text(candidate["text"])

                        if not is_target_location(text, url):
                            continue

                        rooms = extract_room_count(text, url)

                        room_filter = source.get("rooms")

                        if (
                            room_filter
                            and rooms not in room_filter
                        ):
                            continue

                        key = (
                            source["key"],
                            listing_id,
                        )

                        current = found.get(key)

                        if (
                            current
                            and len(current["summary"]) >= len(text)
                        ):
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
                            "summary": text[:700],
                        }

                except PlaywrightTimeoutError:
                    errors.append(
                        f"{source['key']} "
                        f"page {page_number}: timeout"
                    )

                except Exception as exc:
                    errors.append(
                        f"{source['key']} "
                        f"page {page_number}: "
                        f"{type(exc).__name__}: {exc}"
                    )

                finally:
                    await page.close()

        await context.close()
        await browser.close()

    return list(found.values()), errors


def default_state() -> dict[str, Any]:
    return {
        "initialized": False,
        "seen": {
            source["key"]: []
            for source in SOURCES
        },
        "max_ids": {
            source["key"]: 0
            for source in SOURCES
        },
        "heartbeat_week": "",
    }


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return default_state()

    try:
        state = json.loads(
            STATE_PATH.read_text(encoding="utf-8")
        )
    except (json.JSONDecodeError, OSError):
        return default_state()

    template = default_state()

    for key, value in template.items():
        state.setdefault(key, value)

    for source in SOURCES:
        state["seen"].setdefault(
            source["key"],
            [],
        )
        state["max_ids"].setdefault(
            source["key"],
            0,
        )

    return state


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )


def send_telegram(text: str) -> None:
    data = urllib.parse.urlencode(
        {
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "false",
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data=data,
        method="POST",
    )

    with urllib.request.urlopen(
        request,
        timeout=30,
    ) as response:
        if response.status != 200:
            raise RuntimeError(
                f"Telegram returned HTTP {response.status}"
            )


def format_listing(item: dict[str, Any]) -> str:
    rooms = item["rooms"] if item["rooms"] is not None else "—"

    safe_url = html.escape(
        item["url"],
        quote=True,
    )

    return (
        "🏠 <b>ახალი განცხადება</b>\n\n"
        f"🌐 <b>საიტი:</b> "
        f"{html.escape(item['site'])}\n"
        "🔑 <b>გარიგება:</b> ქირავდება\n"
        f"📍 <b>ქალაქი:</b> "
        f"{html.escape(item['location'])}\n"
        f"🚪 <b>ოთახები:</b> {rooms}\n"
        f"📐 <b>ფართობი:</b> "
        f"{html.escape(item['area'])}\n"
        f"💰 <b>ფასი:</b> "
        f"{html.escape(item['price'])}\n\n"
        f"🔗 <a href=\"{safe_url}\">"
        f"განცხადების გახსნა</a>"
    )


async def main() -> int:
    if not TOKEN or not CHAT_ID:
        print(
            "Missing TELEGRAM_BOT_TOKEN "
            "or TELEGRAM_CHAT_ID",
            file=sys.stderr,
        )
        return 2

    state = load_state()
    listings, errors = await scrape_all_sources()

    if errors:
        print("Errors:")

        for error in errors:
            print(f"- {error}")

    if not listings and errors:
        return 1

    new_items: list[dict[str, Any]] = []
    initialized = bool(state["initialized"])

    for item in listings:
        key = item["source_key"]
        listing_id = item["listing_id"]
        seen = state["seen"][key]

        if listing_id not in seen:
            if (
                initialized
                and int(listing_id)
                > int(state["max_ids"][key])
            ):
                new_items.append(item)

            seen.append(listing_id)
            state["seen"][key] = seen[-5000:]

        state["max_ids"][key] = max(
            int(state["max_ids"][key]),
            int(listing_id),
        )

    if not initialized:
        state["initialized"] = True

        send_telegram(
            "✅ მონიტორინგი ჩართულია.\n\n"
            "ვაკონტროლებ MyHome.ge-ზე თბილისში გასაქირავებელ "
            "ბინებს, კერძო სახლებსა და აგარაკებს — 400$-დან, "
            "მხოლოდ ფიზიკური პირების განცხადებებს.\n\n"
            "არსებული განცხადებები შენახულია; შეტყობინებები "
            "მოვა მხოლოდ ახალი განცხადებების დამატებისას."
        )

    else:
        new_items.sort(
            key=lambda item: int(item["listing_id"])
        )

        for item in new_items:
            send_telegram(format_listing(item))

    state["heartbeat_week"] = datetime.now(
        timezone.utc
    ).strftime("%G-W%V")

    save_state(state)

    print(
        f"Found {len(listings)} listings; "
        f"sent {len(new_items)} notifications"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

playwright==1.62.0

{
  "initialized": false,
  "seen": {
    "myhome_rent": []
  },
  "max_ids": {
    "myhome_rent": 0
  },
  "heartbeat_week": ""
}
