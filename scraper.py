# scraper_parallel_video_id_fixed.py
from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs
from concurrent.futures import ThreadPoolExecutor
import re
import sheets

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v")

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

def wait_for_video_id(page, get_video_id, max_seconds=60):
    waited = 0
    while waited < max_seconds:
        vid = get_video_id()
        if vid and vid != "N/A":
            return vid
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
                        if box and box["y"] >= 0:
                            x = box["x"] + box["width"]/2
                            y = box["y"] + box["height"]/2
                            page.mouse.click(x, y)
                            page.wait_for_timeout(1500)
                            return True
                    except:
                        continue
        except:
            continue
    viewport = page.viewport_size or {"width":1366,"height":768}
    page.mouse.click(viewport["width"]/2, viewport["height"]/2)
    page.wait_for_timeout(1500)
    return False

def scrape_single_url(row_num, url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width":1366,"height":768})
        page = context.new_page()
        captured = {"video_id": "N/A"}

        def handle_request(req):
            vid = extract_video_id_from_url(req.url)
            if vid and captured["video_id"]=="N/A":
                captured["video_id"] = vid

        page.on("request", handle_request)
        page.on("response", handle_request)

        try:
            print(f"\n--- Processing Row {row_num}: {url} ---")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

            # Skip non-video ads
            if page.locator("video, creative-preview").count() == 0:
                print(f"⏭ Row {row_num} skipped, non-video ad")
                return

            # First click attempt
            click_possible_video_targets(page)
            video_id = wait_for_video_id(page, lambda: captured["video_id"], max_seconds=60)

            # Second click retry for slow-loading video ads
            if video_id == "N/A":
                page.mouse.wheel(0, 400)
                page.wait_for_timeout(1000)
                click_possible_video_targets(page)
                video_id = wait_for_video_id(page, lambda: captured["video_id"], max_seconds=30)

            # Fallback to performance entries
            if video_id == "N/A":
                video_id = scan_browser_performance_for_video(page)

            # Only write Video ID
            data = ["N/A","N/A",url,"N/A",video_id]
            sheets.update_row(row_num, data)
            print(f"✅ Row {row_num} saved: Video ID = {video_id}")

        except Exception as e:
            print(f"❌ Error processing row {row_num}: {e}")
        finally:
            page.close()
            context.close()
            browser.close()

def run_scraper_parallel(max_workers=3):
    urls = sheets.get_urls()
    url_rows = [(i+2, u.strip()) for i,u in enumerate(urls) if u.strip()]
    if not url_rows:
        print("No URLs to process.")
        return

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(scrape_single_url, row_num, url) for row_num, url in url_rows]
        for f in futures:
            f.result()

    print("\n✅ Finished processing all URLs")

if __name__=="__main__":
    run_scraper_parallel(max_workers=3)