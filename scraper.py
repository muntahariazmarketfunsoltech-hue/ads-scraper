from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs, unquote
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


def get_raw_install_href(page):
    """Get the raw href from the install/download button anchor inside the ad iframe."""
    for frame in page.frames:
        try:
            href = frame.eval_on_selector(
                "a.install-button-anchor, a[id*='install-button'], a[class*='install-button']",
                "el => el.getAttribute('href')"
            )
            if href and href.strip():
                return href.strip()
        except Exception:
            continue

    # Fallback: any anchor pointing to googleleadservices or store
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
                if "googleleadservices.com" in low or "play.google.com/store" in low or "apps.apple.com" in low:
                    return href.strip()
        except Exception:
            continue
    return None


def click_install_button_and_capture(page, context):
    """
    Actually click the Install/Download button inside the ad iframe and
    intercept the new tab / navigation it triggers — that URL IS the final
    app store link, no parsing needed.
    """
    app_url = {"value": None}

    # Listen for any new page (new tab) opened by the click
    def on_page(new_page):
        try:
            new_page.wait_for_load_state("commit", timeout=15000)
            url = new_page.url
            if url and url != "about:blank":
                app_url["value"] = url
            new_page.close()
        except Exception:
            try:
                url = new_page.url
                if url and url != "about:blank":
                    app_url["value"] = url
                new_page.close()
            except Exception:
                pass

    context.on("page", on_page)

    clicked = False
    for frame in page.frames:
        try:
            anchor = frame.locator(
                "a.install-button-anchor, a[id*='install-button'], a[class*='install-button']"
            ).first
            if anchor.count() == 0:
                continue
            anchor.scroll_into_view_if_needed(timeout=3000)
            # Open in same tab so we capture the navigation
            frame.evaluate(
                "el => { el.removeAttribute('target'); }",
                anchor.element_handle()
            )
            anchor.click(timeout=5000)
            clicked = True
            break
        except Exception:
            continue

    if not clicked:
        # Try generic install/download button text
        for frame in page.frames:
            try:
                btn = frame.locator("text=Install, text=Download").first
                if btn.count() == 0:
                    continue
                btn.scroll_into_view_if_needed(timeout=2000)
                btn.click(timeout=5000)
                clicked = True
                break
            except Exception:
                continue

    if clicked:
        # Wait for new tab to appear and be captured
        page.wait_for_timeout(4000)

    context.remove_listener("page", on_page)
    return app_url["value"]


def parse_app_link_from_href(raw_href):
    """
    Properly parse the googleleadservices URL to extract the destination
    app store URL. The actual destination is in the 'adurl' query parameter
    (URL-encoded). We decode it layer by layer until we find a Play Store
    or App Store URL.
    """
    if not raw_href:
        return None

    # Decode up to 3 layers
    text = raw_href
    for _ in range(3):
        text = unquote(text)

    # Now look for play.google.com or apps.apple.com in the decoded string
    # Match the full URL up to a whitespace, quote, or unrelated param
    play_match = re.search(
        r'https://play\.google\.com/store/apps/details\?id=([a-zA-Z][a-zA-Z0-9_.]+)',
        text
    )
    if play_match:
        pkg = play_match.group(1).rstrip("&%+")
        # Trim any junk after the package name (package names are only word chars + dots)
        pkg = re.match(r'[a-zA-Z][a-zA-Z0-9_.]+', pkg).group(0).rstrip(".")
        return f"https://play.google.com/store/apps/details?id={pkg}"

    apple_match = re.search(
        r'https://apps\.apple\.com/[a-zA-Z0-9/\-?=&_.%]+',
        text
    )
    if apple_match:
        url = apple_match.group(0)
        # Cut at first unrelated character
        url = re.split(r'["\'\s<>]', url)[0]
        return url

    return None


def extract_app_store_link(page, context):
    """
    Main entry point. Uses two strategies and returns the best result:
    1. Click the install button and intercept where it actually navigates (most accurate).
    2. Parse the href directly (fast fallback).
    """
    # Strategy 1: click and intercept navigation — most accurate
    clicked_url = click_install_button_and_capture(page, context)
    if clicked_url:
        low = clicked_url.lower()
        if "play.google.com/store" in low or "apps.apple.com" in low:
            # Clean up — keep only the id= param for Play Store
            if "play.google.com" in low:
                pkg_match = re.search(r'[?&]id=([a-zA-Z][a-zA-Z0-9_.]+)', clicked_url)
                if pkg_match:
                    pkg = pkg_match.group(1).rstrip("&%+.")
                    return f"https://play.google.com/store/apps/details?id={pkg}"
            return clicked_url

    # Strategy 2: parse the href directly
    raw_href = get_raw_install_href(page)
    print(f"    raw href (parse fallback): {(raw_href or '')[:120]}")
    parsed = parse_app_link_from_href(raw_href)
    if parsed:
        return parsed

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

            # Scroll creative preview into view
            try:
                page.locator("creative-preview").first.scroll_into_view_if_needed(timeout=3000)
                page.wait_for_timeout(1000)
            except:
                pass

            # Video extraction
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

            # ── Extract accurate app store link ────────────────────────────
            app_link = extract_app_store_link(page, context)
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