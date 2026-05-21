from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor
import re
import sheets
import config

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v")

def clean_text(value):
    if not value:
        return "N/A"
    return re.sub(r"\s+", " ", value).strip() or "N/A"

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
        if "youtube.com/embed/" in url_lower:
            return req_url.split("youtube.com/embed/")[1].split("?")[0].split("&")[0]
        if "youtube.com/watch" in url_lower:
            return query.get("v", [None])[0]
        if "youtu.be/" in url_lower:
            return req_url.split("youtu.be/")[1].split("?")[0].split("&")[0]
    except Exception:
        return None
    return None

def wait_for_video_id(page, captured, max_seconds=25):
    waited = 0
    while waited < max_seconds:
        if captured["video_id"] != "N/A":
            return captured["video_id"]
        page.wait_for_timeout(500)
        waited += 0.5
    return "N/A"

def scan_browser_performance_for_video(page):
    try:
        urls = page.evaluate("() => performance.getEntriesByType('resource').map(r => r.name)")
        for u in urls:
            vid = extract_video_id_from_url(u)
            if vid:
                return vid
    except Exception:
        pass
    return "N/A"

def click_possible_video_targets(page):
    selectors = [
        "iframe", "creative-preview", "video",
        'button[aria-label*="Play"]', 'button[title*="Play"]', 'img[src*="play"]'
    ]
    for sel in selectors:
        try:
            elements = page.locator(sel)
            for i in range(elements.count()):
                el = elements.nth(i)
                if el.is_visible():
                    try:
                        el.scroll_into_view_if_needed(timeout=2000)
                        box = el.bounding_box()
                        if box and box["y"] >= 0 and box["width"] >= 180 and box["height"] >= 120:
                            x = box["x"] + box["width"] / 2
                            y = box["y"] + box["height"] / 2
                            page.mouse.click(x, y)
                            page.wait_for_timeout(1500)
                            return True
                    except:
                        continue
        except:
            continue
    viewport = page.viewport_size or {"width": 1366, "height": 768}
    page.mouse.click(viewport["width"] / 2, viewport["height"] / 2)
    page.wait_for_timeout(1500)
    return False


# ── NEW: extract install/download link from the ad iframe ──────────────────
def extract_app_link(page):
    """
    The Install / Download button lives inside the adframe iframe.
    Its anchor has class containing 'install-button-anchor' and carries
    the real destination URL in the href attribute.
    We try every frame on the page so it works regardless of iframe nesting.
    """
    # Strategy 1: search every frame for the install-button-anchor
    for frame in page.frames:
        try:
            href = frame.eval_on_selector(
                "a.install-button-anchor, a[id*='install-button'], a[class*='install-button']",
                "el => el.getAttribute('href')"
            )
            if href and href.strip() and href.strip() != "N/A":
                return href.strip()
        except Exception:
            continue

    # Strategy 2: look for any anchor whose href points to googleleadservices
    #             or a known app-store URL (play.google.com / apps.apple.com)
    for frame in page.frames:
        try:
            hrefs = frame.eval_on_selector_all(
                "a[href]",
                "els => els.map(el => el.getAttribute('href'))"
            )
            for href in hrefs:
                if not href:
                    continue
                low = href.lower()
                if (
                    "googleleadservices.com" in low
                    or "play.google.com/store" in low
                    or "apps.apple.com" in low
                    or "app.adjust.com" in low
                    or "onelink" in low
                ):
                    return href.strip()
        except Exception:
            continue

    return "N/A"
# ──────────────────────────────────────────────────────────────────────────


def scrape_single_url(url_row):
    row_num, url = url_row
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1366, "height": 768})
        page = context.new_page()
        captured = {"video_id": "N/A"}

        def handle_request(req):
            vid = extract_video_id_from_url(req.url)
            if vid and captured["video_id"] == "N/A":
                captured["video_id"] = vid

        page.on("request", handle_request)
        page.on("response", handle_request)

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            # Scroll creative preview into view
            try:
                page.locator("creative-preview").first.scroll_into_view_if_needed(timeout=3000)
                page.wait_for_timeout(1000)
            except:
                pass

            # Automatic clicks with retries
            click_possible_video_targets(page)
            video_id = wait_for_video_id(page, captured, max_seconds=15)
            if video_id == "N/A":
                video_id = scan_browser_performance_for_video(page)

            # Second click attempt
            if video_id == "N/A":
                page.mouse.wheel(0, 300)
                page.wait_for_timeout(1000)
                click_possible_video_targets(page)
                video_id = wait_for_video_id(page, captured, max_seconds=10)
                if video_id == "N/A":
                    video_id = scan_browser_performance_for_video(page)

            # ── Extract install link ──────────────────────────────────────
            app_link = extract_app_link(page)
            print(f"🔗 Row {row_num} app_link: {app_link}")
            # ─────────────────────────────────────────────────────────────

            advertiser = "N/A"
            ad_name    = "N/A"

            if video_id != "N/A":
                data = [advertiser, ad_name, url, app_link, video_id]
                sheets.update_row(row_num, data)
                print(f"✅ Row {row_num} saved | video_id: {video_id} | app_link: {app_link}")
            else:
                # Still save the row even if no video — so app_link isn't lost
                data = [advertiser, ad_name, url, app_link, "N/A"]
                sheets.update_row(row_num, data)
                print(f"⏭ Row {row_num} — no video found | app_link: {app_link}")

        except Exception as e:
            print(f"❌ Error row {row_num}: {e}")
        finally:
            page.close()
            context.close()
            browser.close()


def run_parallel_scraper(max_workers=3):
    urls = sheets.get_urls()
    urls = [(i + 2, u.strip()) for i, u in enumerate(urls) if u.strip()]
    if not urls:
        print("No URLs to process.")
        return

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(scrape_single_url, url_row) for url_row in urls]
        for future in futures:
            future.result()
    print("✅ Finished processing all URLs")


if __name__ == "__main__":
    run_parallel_scraper(max_workers=3)