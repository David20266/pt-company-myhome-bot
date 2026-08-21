import asyncio
from typing import Any

import check_once as core
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


MAX_PAGE_ATTEMPTS = 5
PAGE_READY_POLL_ATTEMPTS = 5
PAGE_READY_POLL_INTERVAL_MS = 2_000


async def resilient_scrape_all_sources():
    """Scrape MyHome while treating intermittent empty renders as retriable.

    The existing safety contract is preserved: if a page is still empty after
    all attempts, the page is returned in empty_pages and core.main() refuses
    to advance state.json.
    """
    found: dict[tuple[str, str], dict[str, Any]] = {}
    errors: list[str] = []
    empty_pages: list[str] = []

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

        for source in core.SOURCES:
            page_count = int(source.get("pages", 1))
            for page_number in range(1, page_count + 1):
                print(f"Checking {source['key']} page {page_number}")
                requested_url = core.page_url(source["url"], page_number)
                candidates: list[dict[str, str]] = []
                page_listing_ids: set[str] = set()
                final_error = ""

                for attempt in range(1, MAX_PAGE_ATTEMPTS + 1):
                    # A fresh Page for every retry avoids carrying a partially
                    # rendered document/session state into the next attempt.
                    page = await context.new_page()
                    try:
                        response = await page.goto(
                            requested_url,
                            wait_until="domcontentloaded",
                            timeout=90_000,
                        )

                        if response is not None and response.status >= 400:
                            final_error = f"HTTP {response.status}"
                            print(
                                f"{source['key']} page {page_number}: "
                                f"{final_error} (attempt {attempt}/{MAX_PAGE_ATTEMPTS})"
                            )
                        else:
                            # Trigger lazy rendering, then poll the actual DOM
                            # for listing links instead of trusting a fixed sleep.
                            for _ in range(3):
                                await page.mouse.wheel(0, 1600)
                                await page.wait_for_timeout(500)

                            for poll in range(1, PAGE_READY_POLL_ATTEMPTS + 1):
                                candidates = await core.collect_page_links(page)
                                page_listing_ids = {
                                    listing_id
                                    for item in candidates
                                    if (
                                        listing_id := core.listing_id_from_url(
                                            source["site"], item["href"]
                                        )
                                    )
                                }
                                if page_listing_ids:
                                    break
                                if poll < PAGE_READY_POLL_ATTEMPTS:
                                    await page.wait_for_timeout(PAGE_READY_POLL_INTERVAL_MS)

                            print(
                                f"{source['key']} page {page_number}: "
                                f"{len(page_listing_ids)} listing IDs detected "
                                f"(attempt {attempt}/{MAX_PAGE_ATTEMPTS})"
                            )

                            if page_listing_ids:
                                final_error = ""
                                break

                            final_error = "empty result"

                    except PlaywrightTimeoutError:
                        final_error = "timeout"
                        print(
                            f"{source['key']} page {page_number}: timeout "
                            f"(attempt {attempt}/{MAX_PAGE_ATTEMPTS})"
                        )
                    except Exception as exc:
                        final_error = f"{type(exc).__name__}: {exc}"
                        print(
                            f"{source['key']} page {page_number}: {final_error} "
                            f"(attempt {attempt}/{MAX_PAGE_ATTEMPTS})"
                        )
                    finally:
                        await page.close()

                    if attempt < MAX_PAGE_ATTEMPTS:
                        backoff_ms = 2_000 * attempt
                        print(
                            f"{source['key']} page {page_number}: "
                            f"retrying with a fresh page in {backoff_ms // 1000}s"
                        )
                        await asyncio.sleep(backoff_ms / 1000)

                if not page_listing_ids:
                    if final_error and final_error != "empty result":
                        errors.append(
                            f"{source['key']} page {page_number}: {final_error} "
                            f"after {MAX_PAGE_ATTEMPTS} attempts"
                        )
                    else:
                        empty_pages.append(f"{source['key']} page {page_number}")
                    continue

                for candidate in candidates:
                    url = candidate["href"].split("#", 1)[0]
                    listing_id = core.listing_id_from_url(source["site"], url)
                    if not listing_id:
                        continue
                    text = core.normalize_text(candidate["text"])
                    if not core.is_target_location(text, url):
                        continue
                    rooms = core.extract_room_count(text, url)
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
                        "price": core.extract_price(text),
                        "area": core.extract_area(text),
                        "location": core.extract_location(text),
                        "image_url": candidate.get("imageUrl", ""),
                        "summary": text[:700],
                    }

        await context.close()
        await browser.close()

    return list(found.values()), errors, empty_pages


async def main() -> int:
    core.scrape_all_sources = resilient_scrape_all_sources
    return await core.main()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
