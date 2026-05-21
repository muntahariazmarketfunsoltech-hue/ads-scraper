# scraper_full_parallel_with_app_link.py
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
    except:
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
    except:
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
                            x = box["x"] + box["width"]/2
                            y = box["y"] + box["height"]/2
                            page.mouse.click(x, y)
                            page.wait_for_timeout(1500)
                            return True
                    except:
                        continue
        except:
            continue
    viewport = page.viewport_size or {"width": 1366, "height": 768}
    page.mouse.click(viewport["width"]/2, viewport["height"]/2)
    page.wait_for_timeout(1500)
    return False

def extract_advertiser_and_title(page):
    advertiser_selectors = [
        '[data-testid*="advertiser"]',
        '[aria-label*="Advertiser"]',
        'div[role="heading"]:has-text("Advertiser")',
        'a[href*="/advertiser/"]',
        'span:has-text("Advertiser")'
    ]
    title_selectors = [
        '[data-testid*="title"]',
        '[aria-label*="Ad"]',
        'div[role="heading"]:not([aria-label*="Advertiser"])',
        'h1', 'h2', 'span[role="heading"]'
    ]

    def safe_first_text(selectors, frame=None):
        for sel in selectors:
            try:
                loc = (frame or page).locator(sel).first
                if loc.count() > 0 and loc.is_visible():
                    txt = loc.inner_text(timeout=2000)
                    txt = re.sub(r"\s+", " ", txt).strip()
                    if txt:
                        return txt
            except:
                continue
        return "N/A"

    advertiser = safe_first_text(advertiser_selectors)
    title = safe_first_text(title_selectors)

    # Check if inside iframes
    for f in page.frames:
        adv_in_frame = safe_first_text(advertiser_selectors, frame=f)
        if adv_in_frame != "N/A":
            advertiser = adv_in_frame
        title_in_frame = safe_first_text(title_selectors, frame=f)
        if title_in_frame != "N/A":
            title = title_in_frame

    return advertiser, title

def extract_app_link(page):
    try:
        # Look for install button or app store links
        install_anchor = page.locator(
            "a[id*='install-button'], a[href*='play.google.com'], a[href*='apps.apple.com']"
        ).first
        if install_anchor.count() > 0:
            href = install_anchor.get_attribute("href")
            if href:
                return href.strip()
    except:
        pass
    return "N/A"

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

            try:
                page.locator("div[role='main']").first.scroll_into_view_if_needed(timeout=3000)
                page.wait_for_timeout(1000)
            except:
                pass

            click_possible_video_targets(page)
            video_id = wait_for_video_id(page, captured, max_seconds=15)
            if video_id == "N/A":
                video_id = scan_browser_performance_for_video(page)

            if video_id == "N/A":
                page.mouse.wheel(0, 300)
                page.wait_for_timeout(1000)
                click_possible_video_targets(page)
                video_id = wait_for_video_id(page, captured, max_seconds=10)
                if video_id == "N/A":
                    video_id = scan_browser_performance_for_video(page)

            advertiser, ad_name = extract_advertiser_and_title(page)
            app_link = extract_app_link(page)

            if video_id != "N/A":
                data = [advertiser, ad_name, url, app_link, video_id]
                sheets.update_row(row_num, data)
                print(f"✅ Row {row_num} saved: {advertiser}, {ad_name}, {app_link}, {video_id}")
            else:
                print(f"⏭ Row {row_num} skipped, no video found")

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

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(scrape_single_url, url_row) for url_row in urls]
        for future in futures:
            future.result()

    print("✅ Finished processing all URLs")

if __name__ == "__main__":
    run_parallel_scraper(max_workers=3)