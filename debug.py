# debug_scraper_auto_click.py
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from urllib.parse import urlparse, parse_qs
from datetime import datetime
import re
import sheets
import config

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v")

def clean_text(value):
    if not value:
        return "N/A"
    value = re.sub(r"\s+", " ", value).strip()
    return value if value else "N/A"

def extract_video_id_from_url(req_url):
    try:
        parsed = urlparse(req_url)
        query = parse_qs(parsed.query)
        if "youtube.com/embed/" in req_url:
            return req_url.split("youtube.com/embed/")[1].split("?")[0].split("&")[0]
        if "youtube.com/watch" in req_url:
            return query.get("v", [None])[0]
        if "youtu.be/" in req_url:
            return req_url.split("youtu.be/")[1].split("?")[0].split("&")[0]
        if "videoplayback" in req_url:
            for key in ["id", "docid", "video_id", "v"]:
                vid = query.get(key, [None])[0]
                if vid:
                    return vid
            return "google_videoplayback_detected"
        clean_url = req_url.lower().split("?")[0]
        if any(clean_url.endswith(ext) for ext in VIDEO_EXTENSIONS):
            return req_url.split("/")[-1].split("?")[0]
        if ".m3u8" in req_url.lower():
            return req_url.split("/")[-1].split("?")[0]
    except Exception:
        return None
    return None

def wait_for_video_id(page, get_video_id, max_seconds=30):
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
                print(f"🎥 Video found from performance scan: {vid}")
                return vid
    except Exception:
        pass
    return "N/A"

def click_possible_video_targets(page):
    """
    Automatic clicks for video ads.
    Tries iframes, creative-preview, video elements, and play buttons.
    """
    selectors = [
        "iframe",
        "creative-preview",
        "video",
        'button[aria-label*="Play"]',
        'button[title*="Play"]',
        'img[src*="play"]'
    ]

    for sel in selectors:
        try:
            elements = page.locator(sel)
            count = elements.count()
            for i in range(count):
                el = elements.nth(i)
                if el.is_visible():
                    try:
                        el.scroll_into_view_if_needed(timeout=2000)
                        box = el.bounding_box()
                        if box and box["y"] >= 0 and box["width"]>=180 and box["height"]>=120:
                            x = box["x"] + box["width"]/2
                            y = box["y"] + box["height"]/2
                            print(f"▶ Automatically clicking target at {int(x)},{int(y)}")
                            page.mouse.click(x, y)
                            page.wait_for_timeout(1500)
                            return True
                    except Exception:
                        continue
        except Exception:
            continue

    # Fallback click center of viewport
    viewport = page.viewport_size or {"width": 1366, "height": 768}
    x = viewport["width"]/2
    y = viewport["height"]/2
    print(f"▶ Clicking fallback center at {int(x)},{int(y)}")
    page.mouse.click(x, y)
    page.wait_for_timeout(1500)
    return False

def run_debug_scraper(single_url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=150)
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        page = context.new_page()
        captured = {"video_id":"N/A"}

        # Listen to network requests/responses to catch video
        def handle_request(request):
            print("▶ Request URL:", request.url)
            vid = extract_video_id_from_url(request.url)
            if vid and captured["video_id"]=="N/A":
                captured["video_id"] = vid
                print("🎥 Detected video ID:", vid)

        page.on("request", handle_request)
        page.on("response", handle_request)

        print(f"\n--- Debugging URL: {single_url} ---")
        try:
            page.goto(single_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

            # Scroll creative-preview into view
            try:
                page.locator("creative-preview").first.scroll_into_view_if_needed(timeout=3000)
                page.wait_for_timeout(1000)
            except Exception:
                pass

            # First automatic click
            click_possible_video_targets(page)
            video_id = wait_for_video_id(page, lambda: captured["video_id"], max_seconds=30)

            # Performance scan if video not detected
            if video_id == "N/A":
                video_id = scan_browser_performance_for_video(page)

            # Second automatic click attempt
            if video_id == "N/A":
                print("⏳ No video yet, trying second click...")
                try:
                    page.mouse.wheel(0, 400)
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
                click_possible_video_targets(page)
                video_id = wait_for_video_id(page, lambda: captured["video_id"], max_seconds=30)

            # Final scan before giving up
            if video_id == "N/A":
                print("⏳ Final scan before giving up...")
                page.wait_for_timeout(5000)
                if captured["video_id"] != "N/A":
                    video_id = captured["video_id"]
                else:
                    video_id = scan_browser_performance_for_video(page)

            print(f"✅ Video ID captured: {video_id if video_id != 'N/A' else 'NONE'}")

        except Exception as e:
            print("❌ Error during debug:", e)
        finally:
            page.close()
            browser.close()
            print("🔹 Finished debugging single URL")

if __name__=="__main__":
    test_url = "https://adstransparency.google.com/advertiser/AR04661836496116908033/creative/CR08392446506761715713?region=anywhere"
    run_debug_scraper(test_url)