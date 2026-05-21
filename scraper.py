from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from urllib.parse import urlparse, parse_qs
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
    """
    Extract video IDs from YouTube, Google videoplayback, or direct video files.
    """
    try:
        parsed = urlparse(req_url)
        query = parse_qs(parsed.query)

        if "youtube.com/embed/" in req_url:
            vid = req_url.split("youtube.com/embed/")[1].split("?")[0].split("&")[0]
            return vid or None

        if "youtube.com/watch" in req_url:
            vid = query.get("v", [None])[0]
            return vid

        if "youtu.be/" in req_url:
            vid = req_url.split("youtu.be/")[1].split("?")[0].split("&")[0]
            return vid or None

        if "videoplayback" in req_url:
            for key in ["id", "docid", "video_id", "v"]:
                vid = query.get(key, [None])[0]
                if vid:
                    return vid

            # fallback if videoplayback has no useful id
            return "google_videoplayback_detected"

        clean_url = req_url.lower().split("?")[0]

        if any(clean_url.endswith(ext) for ext in VIDEO_EXTENSIONS):
            return req_url.split("/")[-1].split("?")[0]

        if ".m3u8" in req_url.lower():
            return req_url.split("/")[-1].split("?")[0]

    except Exception:
        return None

    return None


def safe_first_text(page, selectors, timeout=2500):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                text = locator.inner_text(timeout=timeout)
                text = clean_text(text)
                if text != "N/A":
                    return text
        except Exception:
            continue

    return "N/A"


def safe_first_attr(page, selectors, attr="href", timeout=2500):
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                value = locator.get_attribute(attr, timeout=timeout)
                if value:
                    return value
        except Exception:
            continue

    return "N/A"


def extract_advertiser_from_page(page):
    selectors = [
        '[data-testid*="advertiser"]',
        '[aria-label*="Advertiser"]',
        'a[href*="/advertiser/"]',
        'div:has-text("Advertiser")',
        'h1',
        'h2',
    ]

    advertiser = safe_first_text(page, selectors)

    if advertiser != "N/A":
        advertiser = advertiser.replace("Advertiser", "").strip()
        advertiser = clean_text(advertiser)

    return advertiser


def extract_ad_name_from_page(page):
    selectors = [
        'h1',
        'h2',
        '[data-testid*="title"]',
        '[aria-label*="Ad"]',
    ]

    title = safe_first_text(page, selectors)

    bad_titles = [
        "Ad Transparency Center",
        "Google Ads Transparency Center",
        "Ads Transparency Center",
    ]

    if title in bad_titles:
        return "N/A"

    return title


def extract_app_link(page):
    selectors = [
        'a[href*="play.google.com/store/apps/details"]',
        'a[href*="apps.apple.com"]',
        'a[href*="market://details"]',
        'a[href*="play.google.com"]',
    ]

    return safe_first_attr(page, selectors, "href")


def accept_consent_if_present(page):
    consent_selectors = [
        'button:has-text("Accept all")',
        'button:has-text("I agree")',
        'button:has-text("Agree")',
        'button:has-text("Accept")',
    ]

    for selector in consent_selectors:
        try:
            btn = page.locator(selector).first
            if btn.count() > 0 and btn.is_visible(timeout=1500):
                btn.click(timeout=2000)
                page.wait_for_timeout(1000)
                return
        except Exception:
            continue


def remove_blocking_overlays(page):
    try:
        page.evaluate("""
            document.querySelectorAll('*').forEach(el => {
                const style = window.getComputedStyle(el);
                const z = parseInt(style.zIndex || '0', 10);

                if (
                    (style.position === 'fixed' || style.position === 'absolute') &&
                    z > 50 &&
                    (
                        style.opacity === '0' ||
                        style.backgroundColor === 'rgba(0, 0, 0, 0)' ||
                        style.backgroundColor === 'transparent'
                    )
                ) {
                    el.style.pointerEvents = 'none';
                }
            });
        """)
    except Exception:
        pass


def click_possible_video_targets(page):
    targets = [
        page.frame_locator("iframe").locator(".ytp-large-play-button").first,
        page.frame_locator("iframe").locator('button[aria-label*="Play"]').first,
        page.frame_locator("iframe").locator('button[title*="Play"]').first,
        page.frame_locator("iframe").locator('img[src*="play_arrow"]').first,
        page.frame_locator("iframe").locator("video").first,

        page.locator('button[aria-label*="Play"]').first,
        page.locator('button[title*="Play"]').first,
        page.locator('img[src*="play_arrow"]').first,
        page.locator('div[aria-label*="Play"]').first,
        page.locator("video").first,

        page.locator("creative-preview").first,
        page.locator("iframe").last,
        page.locator("iframe").nth(1),
        page.locator("iframe").nth(0),
        page.locator('div[role="main"]').first,
    ]

    for target in targets:
        try:
            if target.count() > 0 and target.is_visible(timeout=1500):
                print("▶ Clicking possible video/play target...")
                target.click(force=True, timeout=3000)
                page.wait_for_timeout(1000)
                return True
        except Exception:
            continue

    try:
        viewport = page.viewport_size or {"width": 1366, "height": 768}
        print("▶ Clicking center fallback...")
        page.mouse.click(viewport["width"] / 2, viewport["height"] / 2)
        page.wait_for_timeout(1000)
        return True
    except Exception:
        return False


def wait_for_video_id(page, get_video_id, max_seconds=20):
    """
    Important:
    Do NOT use time.sleep() here.
    page.wait_for_timeout() allows Playwright network events to fire.
    """
    waited = 0

    while waited < max_seconds:
        video_id = get_video_id()

        if video_id and video_id != "N/A":
            return video_id

        page.wait_for_timeout(500)
        waited += 0.5

    return "N/A"


def run_scraper():
    print("Fetching URLs from Google Sheets...")
    urls = sheets.get_urls()
    urls = [u.strip() for u in urls if u and u.strip()]

    if not urls:
        print("No URLs found to process.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=config.HEADLESS,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )

        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )

        for index, url in enumerate(urls):
            row_num = index + 2
            print(f"\n--- Processing Row {row_num}: {url} ---")

            page = context.new_page()
            captured = {"video_id": "N/A"}

            def save_video_id_from_url(req_url):
                video_id = extract_video_id_from_url(req_url)

                if video_id and captured["video_id"] == "N/A":
                    captured["video_id"] = video_id
                    print(f"🎥 Video request detected: {video_id}")

            def handle_request(request):
                save_video_id_from_url(request.url)

            def handle_response(response):
                save_video_id_from_url(response.url)

            page.on("request", handle_request)
            page.on("response", handle_response)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(4000)

                accept_consent_if_present(page)
                remove_blocking_overlays(page)

                advertiser = extract_advertiser_from_page(page)
                ad_name = extract_ad_name_from_page(page)
                app_link = extract_app_link(page)

                click_possible_video_targets(page)

                video_id = wait_for_video_id(
                    page,
                    lambda: captured["video_id"],
                    max_seconds=getattr(config, "WAIT_TIMEOUT", 20)
                )

                if video_id == "N/A":
                    print("⏳ No video yet, trying second click...")

                    try:
                        page.mouse.wheel(0, 400)
                        page.wait_for_timeout(1000)
                    except Exception:
                        pass

                    remove_blocking_overlays(page)
                    click_possible_video_targets(page)

                    video_id = wait_for_video_id(
                        page,
                        lambda: captured["video_id"],
                        max_seconds=20
                    )

                if video_id == "N/A":
                    print("⏳ Final Playwright-safe check before skipping...")
                    page.wait_for_timeout(8000)
                    video_id = captured["video_id"]

                if video_id != "N/A":
                    data = [advertiser, ad_name, url, app_link, video_id]  # A-E only
                    sheets.update_row(row_num, data)
                    print(f"✅ Video ad saved to sheet row {row_num}")
                else:
                    print("⏭ Static/non-video ad skipped. Nothing written.")

            except PlaywrightTimeoutError:
                print(f"❌ Timeout processing row {row_num}: {url}")

            except Exception as e:
                print(f"❌ Error processing row {row_num}: {e}")

            finally:
                page.close()

        browser.close()
        print("\nFinished processing all URLs.")


if __name__ == "__main__":
    run_scraper()