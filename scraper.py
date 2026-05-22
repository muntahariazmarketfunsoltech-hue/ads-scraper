# scraper_single_browser_parallel.py
from playwright.sync_api import sync_playwright
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, parse_qs
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
    except:
        return None
    return None

def wait_for_video_id(page, captured, max_seconds=20):
    waited = 0
    while waited < max_seconds:
        if captured.get("video_id") and captured["video_id"] != "N/A":
            return captured["video_id"]
        page.wait_for_timeout(500)
        waited += 0.5
    return "N/A"

def click_possible_video_targets(page):
    selectors = ["iframe", "creative-preview", "video",
                 'button[aria-label*="Play"]', 'button[title*="Play"]']
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
                            page.wait_for_timeout(1000)
                            return True
                    except:
                        continue
        except:
            continue
    return False

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

def scrape_single_url(browser, row_num, url):
    context = browser.new_context(viewport={"width":1366,"height":768})
    page = context.new_page()
    captured = {"video_id":"N/A"}

    def handle_request(req):
        vid = extract_video_id_from_url(req.url)
        if vid and captured["video_id"]=="N/A":
            captured["video_id"] = vid

    page.on("request", handle_request)
    page.on("response", handle_request)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        # Skip non-video ads
        if page.locator("video, creative-preview").count() == 0:
            print(f"⏭ Row {row_num} skipped, non-video ad")
            return

        click_possible_video_targets(page)
        video_id = wait_for_video_id(page, captured, max_seconds=20)

        # Retry click if video ID not found
        if video_id == "N/A":
            page.mouse.wheel(0, 400)
            page.wait_for_timeout(1000)
            click_possible_video_targets(page)
            video_id = wait_for_video_id(page, captured, max_seconds=15)

        # Fallback to performance entries
        if video_id == "N/A":
            video_id = scan_browser_performance_for_video(page)

        # Only write video ID
        sheets.update_row(row_num, ["N/A","N/A",url,"N/A",video_id])
        print(f"✅ Row {row_num} saved: Video ID = {video_id}")

    except Exception as e:
        print(f"❌ Error processing row {row_num}: {e}")
    finally:
        page.close()
        context.close()

def run_scraper_parallel(max_workers=3):
    urls = sheets.get_urls()
    url_rows = [(i+2, u.strip()) for i,u in enumerate(urls) if u.strip()]
    if not url_rows:
        print("No URLs to process.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # set False to debug slow ads
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(scrape_single_url, browser, row_num, url) for row_num, url in url_rows]
            for f in futures:
                f.result()
        browser.close()

    print("✅ Finished processing all URLs")

if __name__=="__main__":
    run_scraper_parallel(max_workers=3)