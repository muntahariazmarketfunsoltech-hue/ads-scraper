# debug_scraper_auto_click.py
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from urllib.parse import urlparse, parse_qs, unquote
from collections import Counter
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
    Automatic clicks for video ads to trigger media playback.
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

    viewport = page.viewport_size or {"width": 1366, "height": 768}
    x = viewport["width"]/2
    y = viewport["height"]/2
    print(f"▶ Clicking fallback center at {int(x)},{int(y)}")
    page.mouse.click(x, y)
    page.wait_for_timeout(1500)
    return False

# ---------------- APP LINK EXTRACTION LOGIC ----------------

def clean_store_link(url):
    if not url:
        return None
    low = url.lower()
    if "play.google.com" in low and "/apps/details" in low:
        pkg_match = re.search(r'[?&]id=([a-zA-Z0-9_]+\.[a-zA-Z0-9_.]+)', url)
        if pkg_match:
            return f"https://play.google.com/store/apps/details?id={pkg_match.group(1)}"
    elif "apps.apple.com" in low:
        return re.split(r'["\'\s<>]', url)[0]
    return None

def parse_app_link_from_href(raw_href):
    if not raw_href:
        return None
    text = raw_href
    for _ in range(3):
        text = unquote(text)
    return clean_store_link(text)

def extract_app_link_from_dom(page):
    """
    Hunts down the exact Install <a> tag based on the provided HTML structure
    and extracts the raw href without needing to click it.
    """
    raw_href = None
    
    for frame in page.frames:
        try:
            # Target the exact classes discovered via the inspector
            elements = frame.locator("a.install-button-anchor, a:has(.install-button), a:has(#install-button)")
            for i in range(elements.count()):
                el = elements.nth(i)
                href = el.get_attribute("href")
                if href and ("googleadservices.com" in href or "play.google.com" in href or "apps.apple.com" in href):
                    raw_href = href
                    print(f"▶ Found raw tracking href: {raw_href[:80]}...")
                    break
            if raw_href:
                break
        except Exception:
            continue

    if not raw_href:
        try:
            content = page.content()
            match = re.search(r'href="(https://www\.googleadservices\.com/pagead/aclk[^"]+)"', content)
            if match:
                raw_href = match.group(1)
                print(f"▶ Found raw tracking href via regex: {raw_href[:80]}...")
        except Exception:
            pass

    if raw_href:
        return parse_app_link_from_href(raw_href)
        
    return "N/A"

def scan_html_for_app_link(page):
    try:
        content = page.content()
        play_matches = re.findall(r'https://play\.google\.com/store/apps/details\?id=([a-zA-Z0-9_]+\.[a-zA-Z0-9_.]+)', content)
        if play_matches:
            filtered = [p for p in play_matches if p.lower() != 'com.google.android.gms']
            if filtered:
                most_common_pkg = Counter(filtered).most_common(1)[0][0]
                return f"https://play.google.com/store/apps/details?id={most_common_pkg}"
        
        apple_matches = re.findall(r'https://apps\.apple\.com/[a-zA-Z0-9/\-?=&_.%]+', content)
        if apple_matches:
            return re.split(r'["\'\s<>]', apple_matches[0])[0]
    except Exception:
        pass
    return "N/A"

# ------------------------------------------------------------

def run_debug_scraper(single_url, test_row=None):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=150)
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        page = context.new_page()
        captured = {"video_id":"N/A", "app_link": "N/A"}

        def handle_request(request):
            vid = extract_video_id_from_url(request.url)
            if vid and captured["video_id"]=="N/A":
                captured["video_id"] = vid
                print("🎥 Detected video ID from network:", vid)
                
            if captured["app_link"] == "N/A":
                store_link = clean_store_link(request.url)
                if store_link:
                    captured["app_link"] = store_link
                    print(f"📦 Detected App Link from network: {store_link}")

        page.on("request", handle_request)
        page.on("response", handle_request)

        print(f"\n--- Debugging URL: {single_url} ---")
        try:
            page.goto(single_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

            try:
                page.locator("creative-preview").first.scroll_into_view_if_needed(timeout=3000)
                page.wait_for_timeout(1000)
            except Exception:
                pass

            # ---- FETCH VIDEO ID ----
            click_possible_video_targets(page)
            video_id = wait_for_video_id(page, lambda: captured["video_id"], max_seconds=30)

            if video_id == "N/A":
                video_id = scan_browser_performance_for_video(page)

            if video_id == "N/A":
                print("⏳ No video yet, trying second click...")
                try:
                    page.mouse.wheel(0, 400)
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
                click_possible_video_targets(page)
                video_id = wait_for_video_id(page, lambda: captured["video_id"], max_seconds=30)

            if video_id == "N/A":
                page.wait_for_timeout(5000)
                if captured["video_id"] != "N/A":
                    video_id = captured["video_id"]
                else:
                    video_id = scan_browser_performance_for_video(page)

            print(f"✅ Video ID finalized: {video_id if video_id != 'N/A' else 'NONE'}")

            # ---- FETCH APP LINK ----
            app_link = captured["app_link"]
            
            if app_link == "N/A":
                print("⏳ Extracting link directly from DOM based on HTML structure...")
                app_link = extract_app_link_from_dom(page)
                            
            if app_link == "N/A":
                print("⏳ Scanning raw HTML for naked App Links...")
                app_link = scan_html_for_app_link(page)

            print(f"✅ App Link finalized: {app_link}")

            # ---- UPDATE GOOGLE SHEET ----
            if test_row:
                advertiser = "N/A"
                ad_name    = "N/A"
                data = [advertiser, ad_name, single_url, app_link, video_id]
                sheets.update_row(test_row, data)
                print(f"📝 Wrote to sheet at row {test_row}")

        except Exception as e:
            print("❌ Error during debug:", e)
        finally:
            page.close()
            browser.close()
            print("🔹 Finished debugging single URL")

if __name__=="__main__":
    test_url = "https://adstransparency.google.com/advertiser/AR04661836496116908033/creative/CR00379126873570934785?region=PK"
    run_debug_scraper(test_url, test_row=None)