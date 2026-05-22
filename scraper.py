import asyncio
import re
from playwright.async_api import async_playwright
from urllib.parse import urlparse, parse_qs
import sheets

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v")
MAX_CONCURRENT = 3
SHEET_BATCH_SIZE = 9


def extract_video_id_from_url(req_url):
    try:
        url_lower = req_url.lower()
        parsed = urlparse(req_url)
        query = parse_qs(parsed.query)
        if "videoplayback" in url_lower:
            return query.get("id", [None])[0] or req_url
        for ext in VIDEO_EXTENSIONS:
            if url_lower.endswith(ext):
                return req_url.split("/")[-1].split("?")[0]
        if ".m3u8" in url_lower:
            return req_url.split("/")[-1].split("?")[0]
    except Exception:
        return None
    return None


def extract_youtube_ad_video_id(content):
    patterns = [
        r'"adVideoId"\s*:\s*"([a-zA-Z0-9_-]{11})"',
        r'"adPlacementConfig".*?"videoId"\s*:\s*"([a-zA-Z0-9_-]{11})"',
        r'"linearAd".*?"videoId"\s*:\s*"([a-zA-Z0-9_-]{11})"',
        r'adVideoId["\s:]+([a-zA-Z0-9_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, content, re.DOTALL)
        if match:
            return match.group(1)
    return None


async def wait_for_video_id(page, get_video_id, max_seconds=15):
    """Poll but bail out early as soon as ID is found."""
    waited = 0
    while waited < max_seconds:
        vid = get_video_id()
        if vid and vid != "N/A":
            return vid
        await page.wait_for_timeout(300)  # check every 300ms not 500ms
        waited += 0.3
    return "N/A"


async def scan_browser_performance_for_video(page):
    try:
        urls = await page.evaluate(
            "() => performance.getEntriesByType('resource').map(r => r.name)"
        )
        for u in urls:
            vid = extract_video_id_from_url(u)
            if vid:
                return vid
    except Exception:
        pass
    return "N/A"


async def click_possible_video_targets(page):
    selectors = [
        "video", "creative-preview", "iframe",
        'button[aria-label*="Play"]',
        'button[title*="Play"]',
        'img[src*="play"]',
    ]
    for sel in selectors:
        try:
            elements = page.locator(sel)
            count = await elements.count()
            for i in range(count):
                el = elements.nth(i)
                if await el.is_visible():
                    try:
                        await el.scroll_into_view_if_needed(timeout=1000)
                        box = await el.bounding_box()
                        if box and box["y"] >= 0:
                            x = box["x"] + box["width"] / 2
                            y = box["y"] + box["height"] / 2
                            await page.mouse.click(x, y)
                            await page.wait_for_timeout(800)  # was 1500ms
                            return True
                    except Exception:
                        continue
        except Exception:
            continue
    viewport = page.viewport_size or {"width": 1366, "height": 768}
    await page.mouse.click(viewport["width"] / 2, viewport["height"] / 2)
    await page.wait_for_timeout(800)
    return False


async def scrape_youtube_url(page, captured):
    """Register response listener and extract YouTube ad ID."""

    async def handle_response(response):
        if captured["video_id"] != "N/A":
            return
        try:
            url = response.url
            if "youtubei/v1" in url or "get_video_info" in url:
                body = await response.text()
                ad_id = extract_youtube_ad_video_id(body)
                if ad_id:
                    captured["video_id"] = ad_id
                    return
            vid = extract_video_id_from_url(url)
            if vid:
                captured["video_id"] = vid
        except Exception:
            pass

    page.on("response", handle_response)

    # Click player to trigger ad
    try:
        player = page.locator("#movie_player, .html5-video-player")
        if await player.count() > 0:
            await player.first.click()
    except Exception:
        pass

    # Wait up to 10s for response listener to fire
    video_id = await wait_for_video_id(page, lambda: captured["video_id"], max_seconds=10)
    if video_id != "N/A":
        return video_id

    # Fallback: scan page source
    try:
        content = await page.content()
        ad_id = extract_youtube_ad_video_id(content)
        if ad_id:
            return ad_id
    except Exception:
        pass

    # Fallback: read JS variable directly
    try:
        result = await page.evaluate("""
            () => {
                try {
                    const p = window.ytInitialPlayerResponse;
                    if (!p) return null;
                    const str = JSON.stringify(p);
                    const m = str.match(/"adVideoId":"([a-zA-Z0-9_-]{11})"/);
                    return m ? m[1] : null;
                } catch(e) { return null; }
            }
        """)
        if result:
            return result
    except Exception:
        pass

    return "N/A"


async def scrape_single_url(sem, browser, row_num, url):
    async with sem:
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()
        captured = {"video_id": "N/A", "all_urls": []}

        def handle_request(req):
            captured["all_urls"].append(req.url)
            vid = extract_video_id_from_url(req.url)
            if vid and captured["video_id"] == "N/A":
                captured["video_id"] = vid

        page.on("request", handle_request)

        try:
            print(f"  ▶ Row {row_num}: {url}")

            # "commit" fires as soon as navigation starts — captures requests earlier
            await page.goto(url, wait_until="commit", timeout=30000)

            is_youtube = "youtube.com/watch" in url

            if is_youtube:
                # Small wait for initial XHR responses to fire
                await page.wait_for_timeout(2000)
                video_id = await scrape_youtube_url(page, captured)

            else:
                # Wait 2s then immediately check — don't always wait the full time
                await page.wait_for_timeout(2000)
                vid = captured["video_id"]
                if vid and vid != "N/A":
                    video_id = vid  # already captured from network, skip clicking
                else:
                    await click_possible_video_targets(page)
                    video_id = await wait_for_video_id(
                        page, lambda: captured["video_id"], max_seconds=15
                    )

                # One retry with scroll if still nothing
                if video_id == "N/A":
                    await page.mouse.wheel(0, 400)
                    await page.wait_for_timeout(500)
                    await click_possible_video_targets(page)
                    video_id = await wait_for_video_id(
                        page, lambda: captured["video_id"], max_seconds=10
                    )

                # Performance API fallback
                if video_id == "N/A":
                    video_id = await scan_browser_performance_for_video(page)

            if video_id == "N/A":
                has_video_traffic = any(
                    extract_video_id_from_url(u) for u in captured["all_urls"]
                )
                if not has_video_traffic:
                    print(f"  ⏭  Row {row_num} — no video traffic detected")
                    return row_num, None

            print(f"  ✅ Row {row_num} — Video ID: {video_id}")
            return row_num, ["N/A", "N/A", url, "N/A", video_id]

        except Exception as e:
            print(f"  ❌ Row {row_num} error: {e}")
            return row_num, ["N/A", "N/A", url, "N/A", "N/A"]
        finally:
            await page.close()
            await context.close()


async def write_batch_to_sheets(batch):
    for row_num, data in batch:
        if data is None:
            continue
        for attempt in range(1, 4):
            try:
                await asyncio.to_thread(sheets.update_row, row_num, data)
                print(f"  📝 Sheets: row {row_num} written")
                break
            except Exception as e:
                print(f"  ⚠️  Sheets row {row_num} attempt {attempt}/3: {e}")
                if attempt < 3:
                    await asyncio.sleep(3 * attempt)
                else:
                    print(f"  ❌ Sheets gave up on row {row_num}")


async def run_scraper_async():
    urls = sheets.get_urls()
    url_rows = [(i + 2, u.strip()) for i, u in enumerate(urls) if u.strip()]

    if not url_rows:
        print("No URLs to process.")
        return

    print(f"📋 Found {len(url_rows)} URLs — {MAX_CONCURRENT} browsers running in parallel\n")

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    results_lock = asyncio.Lock()
    pending_results = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        async def task_wrapper(row_num, url):
            result = await scrape_single_url(sem, browser, row_num, url)
            async with results_lock:
                pending_results.append(result)
                if len(pending_results) >= SHEET_BATCH_SIZE:
                    batch = pending_results[:SHEET_BATCH_SIZE]
                    del pending_results[:SHEET_BATCH_SIZE]
                    print(f"\n📤 Writing batch of {len(batch)} rows to Sheets...")
                    await write_batch_to_sheets(batch)
                    print("  ✅ Batch written\n")

        await asyncio.gather(*[
            task_wrapper(row_num, url)
            for row_num, url in url_rows
        ])

        if pending_results:
            print(f"\n📤 Writing final {len(pending_results)} rows to Sheets...")
            await write_batch_to_sheets(pending_results)
            print("  ✅ Final batch written")

        await browser.close()

    print("\n✅ All done.")


if __name__ == "__main__":
    asyncio.run(run_scraper_async())