from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs, unquote
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
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
                try:
                    el.scroll_into_view_if_needed(timeout=1000)
                    box = el.bounding_box()
                    if box and box["width"] >= 100 and box["height"] >= 100:
                        x = box["x"] + box["width"] / 2
                        y = box["y"] + box["height"] / 2
                        page.mouse.click(x, y)
                        page.wait_for_timeout(1000)
                        return True
                except:
                    continue
        except:
            continue
    viewport = page.viewport_size or {"width": 1366, "height": 768}
    page.mouse.click(viewport["width"] / 2, viewport["height"] / 2)
    page.wait_for_timeout(1000)
    return False


def scan_html_for_app_link(page):
    """
    Scans the raw HTML for all Play Store links and returns the most frequent one.
    This bypasses all UI and iframe issues entirely.
    """
    try:
        content = page.content()
        # Strict regex requiring a dot to prevent grabbing developer names
        play_matches = re.findall(r'https://play\.google\.com/store/apps/details\?id=([a-zA-Z0-9_]+\.[a-zA-Z0-9_.]+)', content)
        if play_matches:
            # Filter out standard google services that sometimes appear in tracking code
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


def click_install_button_and_capture(page, context):
    """
    Force-clicks the Install button and captures the resulting URL.
    """
    app_url = {"value": None}

    def on_page(new_page):
        try:
            new_page.wait_for_load_state("domcontentloaded", timeout=10000)
            new_page.wait_for_timeout(2000) 
            if new_page.url and new_page.url != "about:blank":
                app_url["value"] = new_page.url
        except Exception:
            try:
                if new_page.url and new_page.url != "about:blank":
                    app_url["value"] = new_page.url
            except:
                pass
        finally:
            try:
                new_page.close()
            except:
                pass

    context.on("page", on_page)
    clicked = False

    # Force click anything that looks like an install anchor
    for frame in page.frames:
        try:
            ctas = frame.locator("text='Install', text='Download', a.install-button-anchor, a[id*='install-button']")
            for i in range(ctas.count()):
                cta = ctas.nth(i)
                try:
                    cta.scroll_into_view_if_needed(timeout=1000)
                    # force=True bypasses the strict visibility checks
                    cta.click(force=True, timeout=3000)
                    clicked = True
                    break
                except:
                    continue
            if clicked:
                break
        except Exception:
            continue

    if clicked:
        page.wait_for_timeout(4000) 

    context.remove_listener("page", on_page)
    return app_url["value"]

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


def scrape_single_url(url_row):
    row_num, url = url_row
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1366, "height": 768})
        page = context.new_page()
        
        captured = {"video_id": "N/A", "app_link": "N/A"}

        # 1. NETWORK SNIFFING: Catch App links and Videos directly from background traffic
        def handle_request(req):
            req_url = req.url
            
            # Catch Video
            if captured["video_id"] == "N/A":
                vid = extract_video_id_from_url(req_url)
                if vid:
                    captured["video_id"] = vid
            
            # Catch App Link directly from network
            if captured["app_link"] == "N/A":
                store_link = clean_store_link(req_url)
                if store_link:
                    captured["app_link"] = store_link

        page.on("request", handle_request)
        page.on("response", handle_request) # Check responses too in case of redirects

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            try:
                page.locator("creative-preview").first.scroll_into_view_if_needed(timeout=3000)
                page.wait_for_timeout(1000)
            except:
                pass

            # Extract Video via UI interaction
            click_possible_video_targets(page)
            video_id = wait_for_video_id(page, captured, max_seconds=10)
            if video_id == "N/A":
                video_id = scan_browser_performance_for_video(page)

            if video_id == "N/A":
                page.mouse.wheel(0, 300)
                page.wait_for_timeout(1000)
                click_possible_video_targets(page)
                video_id = wait_for_video_id(page, captured, max_seconds=10)
                if video_id == "N/A":
                    video_id = scan_browser_performance_for_video(page)

            # ── EXTACT APP LINK ───────────────────────────────────────────
            app_link = captured["app_link"]
            
            # 2. If Network Sniffing failed, try Force-Clicking the UI
            if app_link == "N/A":
                clicked_url = click_install_button_and_capture(page, context)
                if clicked_url:
                    parsed = parse_app_link_from_href(clicked_url)
                    if parsed:
                        app_link = parsed
                    else:
                        clean_clicked = clean_store_link(clicked_url)
                        if clean_clicked:
                            app_link = clean_clicked
                            
            # 3. If Force-Clicking failed, deep scan the raw HTML
            if app_link == "N/A":
                app_link = scan_html_for_app_link(page)

            print(f"📦 Row {row_num} app_link: {app_link}")
            # ──────────────────────────────────────────────────────────────

            advertiser = "N/A"
            ad_name    = "N/A"

            data = [advertiser, ad_name, url, app_link, video_id]
            sheets.update_row(row_num, data)
            print(f"✅ Row {row_num} saved | video_id: {video_id} | app_link: {app_link}")

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