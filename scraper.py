# scraper_robust_parallel.py
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
    """Extract id from videoplayback requests or fallback to filename"""
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

# ── App link helpers (ported directly from debug_scraper_auto_click.py) ──────

def clean_store_link(url):
    """Return a clean Play Store or App Store URL, or None if not a store link."""
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
    """URL-decode up to 3 times then extract the clean store link."""
    if not raw_href:
        return None
    text = raw_href
    for _ in range(3):
        text = unquote(text)
    return clean_store_link(text)

def extract_app_link_from_dom(page):
    """
    Hunts the Install <a> tag across all frames and extracts its href,
    then decodes the googleadservices redirect to get the real store URL.
    """
    raw_href = None

    for frame in page.frames:
        try:
            elements = frame.locator(
                "a.install-button-anchor, a:has(.install-button), a:has(#install-button)"
            )
            for i in range(elements.count()):
                el = elements.nth(i)
                href = el.get_attribute("href")
                if href and (
                    "googleadservices.com" in href
                    or "play.google.com" in href
                    or "apps.apple.com" in href
                ):
                    raw_href = href
                    print(f"    [DOM] found install href: {raw_href[:80]}...")
                    break
            if raw_href:
                break
        except Exception:
            continue

    # Fallback: regex on raw page HTML
    if not raw_href:
        try:
            content = page.content()
            match = re.search(
                r'href="(https://www\.googleadservices\.com/pagead/aclk[^"]+)"',
                content
            )
            if match:
                raw_href = match.group(1)
                print(f"    [regex] found install href: {raw_href[:80]}...")
        except Exception:
            pass

    if raw_href:
        return parse_app_link_from_href(raw_href)

    return "N/A"

def scan_html_for_app_link(page):
    """Last-resort: scan raw HTML for naked Play Store / App Store URLs."""
    try:
        content = page.content()
        play_matches = re.findall(
            r'https://play\.google\.com/store/apps/details\?id=([a-zA-Z0-9_]+\.[a-zA-Z0-9_.]+)',
            content
        )
        if play_matches:
            filtered = [p for p in play_matches if p.lower() != "com.google.android.gms"]
            if filtered:
                pkg = Counter(filtered).most_common(1)[0][0]
                return f"https://play.google.com/store/apps/details?id={pkg}"

        apple_matches = re.findall(
            r'https://apps\.apple\.com/[a-zA-Z0-9/\-?=&_.%]+',
            content
        )
        if apple_matches:
            return re.split(r'["\'\s<>]', apple_matches[0])[0]
    except Exception:
        pass
    return "N/A"

# ── Video helpers ─────────────────────────────────────────────────────────────

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

# ── Per-URL worker ────────────────────────────────────────────────────────────

def scrape_single_url(url_row):
    row_num, url = url_row
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1366, "height": 768})
        page = context.new_page()
        captured = {"video_id": "N/A", "app_link": "N/A"}

        def handle_request(req):
            # Video ID
            vid = extract_video_id_from_url(req.url)
            if vid and captured["video_id"] == "N/A":
                captured["video_id"] = vid

            # App link via network interception (same as debug.py)
            if captured["app_link"] == "N/A":
                store_link = clean_store_link(req.url)
                if store_link:
                    captured["app_link"] = store_link
                    print(f"    [network] app link: {store_link}")

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

            # ── App link: try DOM extraction first (before any clicks) ──
            app_link = captured["app_link"]

            if app_link == "N/A":
                app_link = extract_app_link_from_dom(page)

            if app_link == "N/A":
                app_link = scan_html_for_app_link(page)

            # ── Video: click to trigger playback ──
            click_possible_video_targets(page)
            video_id = wait_for_video_id(page, captured, max_seconds=15)
            if video_id == "N/A":
                video_id = scan_browser_performance_for_video(page)

            # Second attempt
            if video_id == "N/A":
                page.mouse.wheel(0, 300)
                page.wait_for_timeout(1000)
                click_possible_video_targets(page)
                video_id = wait_for_video_id(page, captured, max_seconds=10)
                if video_id == "N/A":
                    video_id = scan_browser_performance_for_video(page)

            # Re-check app_link after page has fully settled
            if app_link == "N/A":
                app_link = captured["app_link"]   # network may have fired by now
            if app_link == "N/A":
                page.wait_for_timeout(2000)
                app_link = extract_app_link_from_dom(page)
            if app_link == "N/A":
                app_link = scan_html_for_app_link(page)

            advertiser = "N/A"
            ad_name    = "N/A"

            if video_id != "N/A" or app_link != "N/A":
                data = [advertiser, ad_name, url, app_link, video_id]
                sheets.update_row(row_num, data)
                link_preview = (app_link[:80] + "...") if app_link != "N/A" else "N/A"
                print(f"✅ Row {row_num} | video_id: {video_id} | app_link: {link_preview}")
            else:
                print(f"⏭ Row {row_num} skipped — no video or install link found")

        except Exception as e:
            print(f"❌ Error row {row_num}: {e}")
        finally:
            page.close()
            context.close()
            browser.close()

# ── Entry point ───────────────────────────────────────────────────────────────

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