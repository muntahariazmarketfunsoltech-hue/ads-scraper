from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import re
import sheets


VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v", ".m3u8")


def get_exact_time():
    return datetime.now().strftime("%I:%M:%S %p")


def clean_text(value):
    if not value:
        return "N/A"
    return re.sub(r"\s+", " ", value).strip() or "N/A"


def is_real_video_response(response):
    """
    Checks if the browser response is actually video/media.
    """
    try:
        url = response.url.lower()
        headers = response.headers
        content_type = headers.get("content-type", "").lower()

        if content_type.startswith("video/"):
            return True

        if "application/vnd.apple.mpegurl" in content_type:
            return True

        if "application/x-mpegurl" in content_type:
            return True

        if "videoplayback" in url:
            return True

        if any(ext in url for ext in VIDEO_EXTENSIONS):
            return True

    except Exception:
        pass

    return False


def extract_video_id_from_url(req_url):
    """
    Extracts only clean video IDs or filenames.
    Does NOT return full links.
    """
    try:
        url_lower = req_url.lower()
        parsed = urlparse(req_url)
        query = parse_qs(parsed.query)

        if "videoplayback" in url_lower:
            video_id = query.get("id", [None])[0]

            if video_id:
                return video_id

            for key in ["itag", "ei", "source"]:
                value = query.get(key, [None])[0]
                if value:
                    return value

            return None

        for ext in VIDEO_EXTENSIONS:
            if ext in url_lower:
                filename = parsed.path.split("/")[-1]
                filename = filename.split("?")[0].strip()

                if filename:
                    return filename

        if "youtube.com/embed/" in url_lower:
            return req_url.split("youtube.com/embed/")[1].split("?")[0].split("&")[0]

        if "youtube.com/watch" in url_lower:
            return query.get("v", [None])[0]

        if "youtu.be/" in url_lower:
            return req_url.split("youtu.be/")[1].split("?")[0].split("&")[0]

    except Exception:
        return None

    return None


def extract_video_from_dom(page):
    """
    Checks actual video elements on page and inside frames.
    """
    try:
        video_sources = page.evaluate("""
            () => Array.from(document.querySelectorAll('video'))
                .map(v => v.currentSrc || v.src || '')
                .filter(Boolean)
        """)

        for src in video_sources:
            video_id = extract_video_id_from_url(src)
            if video_id:
                return video_id

    except Exception:
        pass

    for frame in page.frames:
        try:
            video_sources = frame.evaluate("""
                () => Array.from(document.querySelectorAll('video'))
                    .map(v => v.currentSrc || v.src || '')
                    .filter(Boolean)
            """)

            for src in video_sources:
                video_id = extract_video_id_from_url(src)
                if video_id:
                    return video_id

        except Exception:
            continue

    return "N/A"


def scan_browser_performance_for_video(page):
    """
    Scans performance entries for real video URLs only.
    """
    try:
        urls = page.evaluate("""
            () => performance.getEntriesByType('resource').map(r => r.name)
        """)

        for u in urls:
            u_lower = u.lower()

            if (
                "videoplayback" in u_lower
                or ".mp4" in u_lower
                or ".webm" in u_lower
                or ".mov" in u_lower
                or ".m4v" in u_lower
                or ".m3u8" in u_lower
                or "youtube.com/embed/" in u_lower
                or "youtube.com/watch" in u_lower
                or "youtu.be/" in u_lower
            ):
                video_id = extract_video_id_from_url(u)

                if video_id:
                    return video_id

    except Exception:
        pass

    return "N/A"


def click_possible_video_targets(page):
    """
    Clicks possible video preview areas.
    Avoids install buttons/app links.
    """
    selectors = [
        "video",
        "iframe",
        "creative-preview",
        'button[aria-label*="Play"]',
        'button[title*="Play"]',
        'div[aria-label*="Play"]',
        'img[src*="play"]'
    ]

    for sel in selectors:
        try:
            elements = page.locator(sel)
            count = elements.count()

            for i in range(count):
                el = elements.nth(i)

                if not el.is_visible():
                    continue

                try:
                    el.scroll_into_view_if_needed(timeout=2000)
                    box = el.bounding_box()

                    if not box:
                        continue

                    if box["width"] < 120 or box["height"] < 80:
                        continue

                    x = box["x"] + box["width"] / 2
                    y = box["y"] + box["height"] / 2

                    page.mouse.click(x, y)
                    page.wait_for_timeout(1500)
                    return True

                except Exception:
                    continue

        except Exception:
            continue

    return False


def wait_for_video_id(page, captured, max_seconds=20):
    waited = 0

    while waited < max_seconds:
        if captured.get("video_id") and captured["video_id"] != "N/A":
            return captured["video_id"]

        dom_video_id = extract_video_from_dom(page)
        if dom_video_id != "N/A":
            return dom_video_id

        page.wait_for_timeout(500)
        waited += 0.5

    return "N/A"


def extract_advertiser_and_title(page):
    advertiser_selectors = [
        '[data-testid*="advertiser"]',
        '[aria-label*="Advertiser"]',
        'a[href*="/advertiser/"]',
        'div[class*="advertiser"]',
        'div[class*="publisher"]'
    ]

    title_selectors = [
        '[data-testid*="title"]',
        'div[role="heading"]:not([aria-label*="Advertiser"])',
        'h1',
        'h2',
        'span[role="heading"]',
        'div[class*="title"]'
    ]

    def safe_first_text(selectors, frame=None):
        target = frame or page

        for sel in selectors:
            try:
                loc = target.locator(sel).first

                if loc.count() > 0 and loc.is_visible():
                    txt = loc.inner_text(timeout=2000)
                    txt = clean_text(txt)

                    if txt != "N/A":
                        return txt

            except Exception:
                continue

        return "N/A"

    advertiser = safe_first_text(advertiser_selectors)
    title = safe_first_text(title_selectors)

    for frame in page.frames:
        adv_in_frame = safe_first_text(advertiser_selectors, frame=frame)
        if adv_in_frame != "N/A":
            advertiser = adv_in_frame

        title_in_frame = safe_first_text(title_selectors, frame=frame)
        if title_in_frame != "N/A":
            title = title_in_frame

    return advertiser, title


def scrape_single_url(url_row):
    row_num, url = url_row

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )

        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )

        page = context.new_page()
        captured = {"video_id": "N/A"}

        def handle_response(response):
            try:
                if not is_real_video_response(response):
                    return

                video_id = extract_video_id_from_url(response.url)

                if video_id and captured["video_id"] == "N/A":
                    captured["video_id"] = video_id

            except Exception:
                pass

        page.on("response", handle_response)

        try:
            original_url = url

            if "region=" not in url:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}region=anywhere"

            print(f"🔍 Checking row {row_num}: {url}")

            sheets.add_log(
                row_number=row_num,
                status="STARTED",
                log_type="VIDEO",
                url=url,
                message="Started checking video ad"
            )

            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)

            video_id = extract_video_from_dom(page)

            if video_id == "N/A":
                click_possible_video_targets(page)
                video_id = wait_for_video_id(page, captured, max_seconds=15)

            if video_id == "N/A":
                video_id = scan_browser_performance_for_video(page)

            if video_id == "N/A":
                page.mouse.wheel(0, 400)
                page.wait_for_timeout(1500)

                click_possible_video_targets(page)
                video_id = wait_for_video_id(page, captured, max_seconds=10)

            # NON-VIDEO ROW SAVE
            if video_id == "N/A":
                video_checked_time = get_exact_time()

                data = [
                    "",
                    "",
                    url,
                    "",
                    "",
                    "NON_VIDEO",
                    video_checked_time
                ]

                sheets.update_video_row(row_num, data)

                sheets.add_log(
                    row_number=row_num,
                    status="NON_VIDEO",
                    log_type="VIDEO",
                    url=url,
                    video_id="NON_VIDEO",
                    message="No video detected"
                )

                print(f"⏭ Row {row_num} marked NON_VIDEO at {video_checked_time}")
                return

            # VIDEO ROW SAVE
            advertiser, ad_name = extract_advertiser_and_title(page)

            video_checked_time = get_exact_time()

            data = [
                advertiser,
                ad_name,
                url,
                "",
                "",
                video_id,
                video_checked_time
            ]

            sheets.update_video_row(row_num, data)

            sheets.add_log(
                row_number=row_num,
                status="SUCCESS",
                log_type="VIDEO",
                url=url,
                video_id=video_id,
                message="Video ID saved"
            )

            print(f"✅ Row {row_num} saved video ID at {video_checked_time}: {video_id}")

        except Exception as e:
            print(f"❌ Error row {row_num}: {e}")

            try:
                sheets.add_log(
                    row_number=row_num,
                    status="ERROR",
                    log_type="VIDEO",
                    url=url,
                    message=str(e)
                )
            except Exception:
                pass

        finally:
            page.close()
            context.close()
            browser.close()


def run_parallel_video_scraper(max_workers=3):
    urls = sheets.get_urls_with_retry()

    url_rows = [
        (i + 2, u.strip())
        for i, u in enumerate(urls)
        if u and u.strip()
    ]

    if not url_rows:
        print("No transparency URLs found in column H.")
        return

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(scrape_single_url, url_row)
            for url_row in url_rows
        ]

        for future in as_completed(futures):
            future.result()

    print("✅ Finished processing video ads")


if __name__ == "__main__":
    run_parallel_video_scraper(max_workers=3)