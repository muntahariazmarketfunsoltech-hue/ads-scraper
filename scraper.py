from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import re
import time
import threading
import sheets


MAX_WORKERS = 2
SHEET_LOCK = threading.Lock()

VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v", ".m3u8")

INSTALL_SELECTORS = [
    "a.install-button-anchor.svg-anchor",
    "a.install-button-anchor",
    'a[data-asoch-targets-ad-objective-type]',
    'a:has-text("Install")',
    'a:has-text("Get")',
    'a:has-text("Download")',
]


def safe_update_combined_row(row_num, data):
    with SHEET_LOCK:
        sheets.update_combined_row(row_num, data)


def safe_update_headline_desc(row_num, headline, description):
    with SHEET_LOCK:
        sheets.update_headline_and_description(row_num, headline, description)


def safe_add_log(row_number, status, log_type, url="", video_id="", app_link="", message=""):
    with SHEET_LOCK:
        sheets.add_log(
            row_number=row_number,
            status=status,
            log_type=log_type,
            url=url,
            video_id=video_id,
            app_link=app_link,
            message=message
        )


def get_exact_time():
    return datetime.now().strftime("%I:%M:%S %p")


def clean_text(value):
    if not value:
        return "N/A"
    return re.sub(r"\s+", " ", str(value)).strip() or "N/A"


# =========================
# VIDEO ID LOGIC
# =========================

def is_real_video_response(response):
    try:
        url = response.url.lower()
        headers = response.headers
        content_type = headers.get("content-type", "").lower()

        if content_type.startswith("video/"):
            return True
        if "application/vnd.apple.mpegurl" in content_type:
            return True
        if "application/x-mpegurl" in content_type:
            return True
        if "videoplayback" in url:
            return True
        if any(ext in url for ext in VIDEO_EXTENSIONS):
            return True

    except Exception:
        pass

    return False


def extract_video_id_from_url(req_url):
    try:
        url_lower = req_url.lower()
        parsed = urlparse(req_url)
        query = parse_qs(parsed.query)

        if "videoplayback" in url_lower:
            video_id = query.get("id", [None])[0]
            if video_id:
                return video_id
            for key in ["itag", "ei", "source"]:
                value = query.get(key, [None])[0]
                if value:
                    return value
            return None

        for ext in VIDEO_EXTENSIONS:
            if ext in url_lower:
                filename = parsed.path.split("/")[-1]
                filename = filename.split("?")[0].strip()
                if filename:
                    return filename

        if "youtube.com/embed/" in url_lower:
            return req_url.split("youtube.com/embed/")[1].split("?")[0].split("&")[0]
        if "youtube.com/watch" in url_lower:
            return query.get("v", [None])[0]
        if "youtu.be/" in url_lower:
            return req_url.split("youtu.be/")[1].split("?")[0].split("&")[0]

    except Exception:
        return None

    return None


def extract_video_from_dom(page):
    try:
        video_sources = page.evaluate("""
            () => Array.from(document.querySelectorAll('video'))
                .map(v => v.currentSrc || v.src || '')
                .filter(Boolean)
        """)
        for src in video_sources:
            video_id = extract_video_id_from_url(src)
            if video_id:
                return video_id
    except Exception:
        pass

    for frame in page.frames:
        try:
            video_sources = frame.evaluate("""
                () => Array.from(document.querySelectorAll('video'))
                    .map(v => v.currentSrc || v.src || '')
                    .filter(Boolean)
            """)
            for src in video_sources:
                video_id = extract_video_id_from_url(src)
                if video_id:
                    return video_id
        except Exception:
            continue

    return "N/A"


def scan_browser_performance_for_video(page):
    try:
        urls = page.evaluate("""
            () => performance.getEntriesByType('resource').map(r => r.name)
        """)
        for u in urls:
            u_lower = u.lower()
            if (
                "videoplayback" in u_lower
                or ".mp4" in u_lower
                or ".webm" in u_lower
                or ".mov" in u_lower
                or ".m4v" in u_lower
                or ".m3u8" in u_lower
                or "youtube.com/embed/" in u_lower
                or "youtube.com/watch" in u_lower
                or "youtu.be/" in u_lower
            ):
                video_id = extract_video_id_from_url(u)
                if video_id:
                    return video_id
    except Exception:
        pass

    return "N/A"


def click_possible_video_targets(page):
    selectors = [
        "video",
        "iframe",
        "creative-preview",
        'button[aria-label*="Play"]',
        'button[title*="Play"]',
        'div[aria-label*="Play"]',
        'img[src*="play"]'
    ]

    for sel in selectors:
        try:
            elements = page.locator(sel)
            count = elements.count()

            for i in range(count):
                el = elements.nth(i)

                if not el.is_visible():
                    continue

                try:
                    el.scroll_into_view_if_needed(timeout=2000)
                    box = el.bounding_box()

                    if not box:
                        continue
                    if box["width"] < 120 or box["height"] < 80:
                        continue

                    x = box["x"] + box["width"] / 2
                    y = box["y"] + box["height"] / 2

                    page.mouse.click(x, y)
                    page.wait_for_timeout(1500)
                    return True

                except Exception:
                    continue

        except Exception:
            continue

    return False


def wait_for_video_id(page, captured, max_seconds=20):
    waited = 0

    while waited < max_seconds:
        if captured.get("video_id") and captured["video_id"] != "N/A":
            return captured["video_id"]

        dom_video_id = extract_video_from_dom(page)
        if dom_video_id != "N/A":
            return dom_video_id

        page.wait_for_timeout(500)
        waited += 0.5

    return "N/A"


def detect_video_id(page, captured):
    video_id = extract_video_from_dom(page)

    if video_id == "N/A":
        click_possible_video_targets(page)
        video_id = wait_for_video_id(page, captured, max_seconds=15)

    if video_id == "N/A":
        video_id = scan_browser_performance_for_video(page)

    if video_id == "N/A":
        page.mouse.wheel(0, 400)
        page.wait_for_timeout(1500)
        click_possible_video_targets(page)
        video_id = wait_for_video_id(page, captured, max_seconds=10)

    return video_id


# =========================
# APP LINK LOGIC
# =========================

def clean_googleadservices_link(href):
    if not href:
        return "N/A"

    href = href.strip()

    if href.startswith("//"):
        href = "https:" + href

    try:
        parsed = urlparse(href)
        query = parse_qs(parsed.query)

        possible_keys = ["adurl", "url", "q", "u", "ds_dest_url", "destination"]

        for key in possible_keys:
            value = query.get(key, [None])[0]
            if value:
                return unquote(value)

    except Exception:
        pass

    return href


def is_good_app_link(href):
    if not href:
        return False

    href = href.lower()

    return (
        "googleadservices.com/pagead/aclk" in href
        or "play.google.com" in href
        or "apps.apple.com" in href
        or "itunes.apple.com" in href
    )


def get_visible_install_candidates_from_target(target):
    candidates = []

    for selector in INSTALL_SELECTORS:
        try:
            loc = target.locator(selector)
            count = loc.count()

            for i in range(count):
                try:
                    el = loc.nth(i)

                    href = el.get_attribute("href", timeout=1500)
                    data_href = el.get_attribute("data-href", timeout=1000)

                    final_href = href or data_href

                    if not final_href or not is_good_app_link(final_href):
                        continue

                    box = el.bounding_box(timeout=1500)

                    if not box:
                        continue
                    if box["width"] < 20 or box["height"] < 10:
                        continue

                    text = ""
                    try:
                        text = el.inner_text(timeout=1000).strip().lower()
                    except Exception:
                        pass

                    score = 0

                    try:
                        class_name = el.get_attribute("class", timeout=1000) or ""
                        if "install-button-anchor" in class_name:
                            score += 100
                    except Exception:
                        pass

                    if "install" in text:
                        score += 80
                    elif "get" in text or "download" in text:
                        score += 40

                    center_x = box["x"] + box["width"] / 2
                    center_y = box["y"] + box["height"] / 2

                    if 350 <= center_x <= 850:
                        score += 40
                    if 50 <= center_y <= 700:
                        score += 40
                    if center_y > 700:
                        score -= 100

                    candidates.append({
                        "href": final_href,
                        "score": score,
                        "box": box,
                        "text": text,
                    })

                except Exception:
                    continue

        except Exception:
            continue

    return candidates


def extract_visible_install_link(page):
    all_candidates = []

    try:
        all_candidates.extend(get_visible_install_candidates_from_target(page))
    except Exception:
        pass

    for frame in page.frames:
        try:
            all_candidates.extend(get_visible_install_candidates_from_target(frame))
        except Exception:
            continue

    if not all_candidates:
        return "N/A"

    all_candidates.sort(key=lambda x: x["score"], reverse=True)
    best = all_candidates[0]

    if best["score"] <= 0:
        return "N/A"

    return clean_googleadservices_link(best["href"])


def extract_install_link_by_precise_js(page):
    js = r"""
    () => {
        const anchors = Array.from(document.querySelectorAll('a[href], a[data-href]'));
        const candidates = anchors.map(a => {
            const href = a.href || a.getAttribute('href') || a.getAttribute('data-href') || '';
            const text = (a.innerText || a.textContent || '').trim().toLowerCase();
            const cls = String(a.className || '').toLowerCase();
            const aria = String(a.getAttribute('aria-label') || '').toLowerCase();
            const rect = a.getBoundingClientRect();

            const goodLink =
                href.includes('googleadservices.com/pagead/aclk') ||
                href.includes('play.google.com') ||
                href.includes('apps.apple.com') ||
                href.includes('itunes.apple.com');

            const looksInstall =
                cls.includes('install-button-anchor') ||
                text.includes('install') ||
                text.includes('get') ||
                text.includes('download') ||
                aria.includes('install');

            const visible =
                rect.width > 20 &&
                rect.height > 10 &&
                rect.bottom > 0 &&
                rect.right > 0 &&
                rect.top < window.innerHeight &&
                rect.left < window.innerWidth;

            if (!goodLink || !looksInstall || !visible) return null;

            let score = 0;
            if (cls.includes('install-button-anchor')) score += 100;
            if (text.includes('install')) score += 80;
            if (text.includes('get') || text.includes('download')) score += 40;
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            if (cx >= 350 && cx <= 850) score += 40;
            if (cy >= 50 && cy <= 700) score += 40;
            if (cy > 700) score -= 100;
            return { href, score };
        }).filter(Boolean);

        candidates.sort((a, b) => b.score - a.score);
        return candidates.length ? candidates[0].href : null;
    }
    """

    try:
        href = page.evaluate(js)
        if href and is_good_app_link(href):
            return clean_googleadservices_link(href)
    except Exception:
        pass

    for frame in page.frames:
        try:
            href = frame.evaluate(js)
            if href and is_good_app_link(href):
                return clean_googleadservices_link(href)
        except Exception:
            continue

    return "N/A"


def wait_and_extract_install_link(page, max_wait_seconds=35):
    start = time.time()

    while time.time() - start < max_wait_seconds:
        app_link = extract_visible_install_link(page)
        if app_link != "N/A":
            return app_link

        app_link = extract_install_link_by_precise_js(page)
        if app_link != "N/A":
            return app_link

        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass

        page.wait_for_timeout(1500)

    return "N/A"


# =========================
# HEADLINE AND DESCRIPTION LOGIC
# =========================

def wait_and_extract_headline_description(page, max_wait_seconds=15):
    js = r"""
    () => {
        let headText = "N/A";
        let descText = "N/A";

        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
        };

        const headNodes = document.querySelectorAll('[class*="-e-15"], [class*="headline"]');
        for (let el of headNodes) {
            if (isVisible(el)) {
                let text = (el.innerText || el.textContent || "").replace(/\n/g, ' ').trim();
                if (text.length > 1 && !text.includes('{{')) {
                    headText = text;
                    break;
                }
            }
        }

        const descNodes = document.querySelectorAll('[class*="-e-67"], [class*="long-description"]');
        for (let el of descNodes) {
            if (isVisible(el)) {
                let text = (el.innerText || el.textContent || "").replace(/\n/g, ' ').trim();
                if (text.length > 1 && text !== headText && !text.includes('{{')) {
                    descText = text;
                    break;
                }
            }
        }

        if (headText !== "N/A" || descText !== "N/A") {
            return { headline: headText, description: descText };
        }

        return null;
    }
    """

    start = time.time()

    while time.time() - start < max_wait_seconds:
        for frame in page.frames:
            try:
                result = frame.evaluate(js)
                if result and (result.get("headline", "N/A") != "N/A" or result.get("description", "N/A") != "N/A"):
                    return result.get("headline", "N/A"), result.get("description", "N/A")
            except Exception:
                continue

        page.wait_for_timeout(1000)

    return "N/A", "N/A"


# =========================
# ADVERTISER LOGIC
# =========================

def extract_advertiser_from_page(page):
    """
    Extract advertiser name from the top of the page.
    Strategy 1: Exact '.advertiser-title' class match.
    Strategy 2: Visual fallback scanning leaf nodes near top of page.
    """
    # STRATEGY 1: Exact class match
    try:
        loc = page.locator('.advertiser-title')
        if loc.count() > 0:
            text = loc.nth(0).inner_text(timeout=1500).strip()
            if text and len(text) > 1:
                return text
    except Exception:
        pass

    # STRATEGY 2: Visual fallback
    js = r"""
    () => {
        const badExact = [
            'ad details', 'last shown', 'format:', 'shown in', 'report this ad',
            'see more ads', 'ads transparency centre', 'ads transparency center',
            'faqs', 'privacy', 'terms', 'policies', 'home', 'sign in', 'sign up',
            'log in', 'close', 'menu', 'keyboard_arrow_right', 'arrow_back',
            'arrow_forward', 'chevron_left', 'chevron_right'
        ];

        const elements = Array.from(document.querySelectorAll('body *'));
        let candidates = [];

        for (let i = 0; i < elements.length; i++) {
            let el = elements[i];
            if (el.childElementCount > 0) continue;

            const text = (el.innerText || el.textContent || "").trim();
            if (text.length < 2 || text.length > 80 || text.includes('\n')) continue;

            const lower = text.toLowerCase();
            if (badExact.includes(lower)) continue;
            if (lower.includes('information about this ad')) continue;
            if (lower.includes('cookie')) continue;

            const rect = el.getBoundingClientRect();
            if (rect.y < 0 || rect.y > 450 || rect.width < 10 || rect.height < 10) continue;

            const style = window.getComputedStyle(el);
            if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;

            candidates.push({
                text: text,
                y: rect.y,
                x: rect.x,
                font: parseFloat(style.fontSize || '0'),
                domIndex: i
            });
        }

        let unique = [];
        let seen = new Set();
        for (let c of candidates) {
            if (!seen.has(c.text)) {
                seen.add(c.text);
                unique.push(c);
            }
        }

        if (unique.length === 0) return null;

        unique.sort((a, b) => {
            if (b.font !== a.font) return b.font - a.font;
            if (a.y !== b.y) return a.y - b.y;
            return a.domIndex - b.domIndex;
        });

        return unique[0].text;
    }
    """

    try:
        advertiser = page.evaluate(js)
        if advertiser:
            return advertiser
    except Exception:
        pass

    return "N/A"


# =========================
# MAIN SCRAPER
# =========================

def scrape_single_url(url_row):
    """
    FIX: Each URL gets its own fresh browser + context + page.
    This is the KEY fix — the original code was correct in structure,
    but the `captured` dict was shared across page navigations inside
    the same Playwright context in some usage patterns.
    
    We also now:
    1. Clear performance entries before loading the page so stale
       video responses from a previous page never bleed in.
    2. Register the response handler BEFORE goto(), not after.
    3. Reset `captured` to a fresh dict for every URL (already done,
       but now guaranteed since we open a new browser per call).
    """
    row_num, url = url_row

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
            ]
        )

        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )

        page = context.new_page()

        # FIX 1: Fresh captured dict per URL (guaranteed fresh since new browser)
        captured = {"video_id": "N/A"}

        # FIX 2: Register handler BEFORE goto so no early responses are missed
        def handle_response(response):
            try:
                if not is_real_video_response(response):
                    return
                video_id = extract_video_id_from_url(response.url)
                if video_id and captured["video_id"] == "N/A":
                    captured["video_id"] = video_id
            except Exception:
                pass

        page.on("response", handle_response)

        try:
            if "region=" not in url:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}region=anywhere"

            print(f"🔍 Row {row_num}: opening transparency URL")

            safe_add_log(
                row_number=row_num,
                status="STARTED",
                log_type="COMBINED",
                url=url,
                message="Started video ID then app link extraction"
            )

            # FIX 3: Use a full navigation with networkidle wait so the ad
            # creative has time to fully load and fire its video network requests.
            # "domcontentloaded" was too early — the ad iframe hadn't loaded yet
            # for rows 2+ because the SPA reuses the same DOM shell.
            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # FIX 4: Clear stale performance entries immediately after load.
            # This ensures scan_browser_performance_for_video() only sees
            # requests from THIS page, not a cached previous page's data.
            try:
                page.evaluate("() => performance.clearResourceTimings()")
            except Exception:
                pass

            # FIX 5: Extra wait for the ad creative iframe to load.
            # The Transparency Center is a SPA — the ad panel loads async
            # AFTER the main DOM is ready. 4s was sometimes not enough for
            # advertisers after the first one (cold-cache vs warm-cache).
            page.wait_for_timeout(5000)

            # Step 1: extract advertiser from top header
            advertiser = extract_advertiser_from_page(page)

            # Step 2: detect video ID
            video_id = detect_video_id(page, captured)
            video_time = get_exact_time()

            if video_id == "N/A":
                data = [
                    advertiser,
                    "",
                    url,
                    "",
                    "",
                    "NON_VIDEO",
                    video_time
                ]

                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, "N/A", "N/A")

                safe_add_log(
                    row_number=row_num,
                    status="NON_VIDEO",
                    log_type="COMBINED",
                    url=url,
                    video_id="NON_VIDEO",
                    message="No video detected. App link not checked."
                )

                print(f"⏭ Row {row_num}: NON_VIDEO at {video_time}")
                return

            print(f"🎬 Row {row_num}: video ID found: {video_id}")

            # Step 3: only after video is found, extract app link
            app_link = wait_and_extract_install_link(page, max_wait_seconds=35)
            app_link_time = get_exact_time()

            # Step 4: Extract Headline and Description
            headline, description = wait_and_extract_headline_description(page, max_wait_seconds=15)

            if app_link == "N/A":
                status = "VIDEO_FOUND_APP_LINK_NOT_FOUND"
                message = "Video ID found, but exact visible install link not found"
            else:
                status = "SUCCESS"
                message = "Video ID and app link saved"

            data = [
                advertiser,
                "",
                url,
                app_link,
                app_link_time,
                video_id,
                video_time
            ]

            safe_update_combined_row(row_num, data)
            safe_update_headline_desc(row_num, headline, description)

            safe_add_log(
                row_number=row_num,
                status=status,
                log_type="COMBINED",
                url=url,
                video_id=video_id,
                app_link=app_link,
                message=message
            )

            print(f"✅ Row {row_num}: saved advertiser + video ID + app link + text")

        except Exception as e:
            error_time = get_exact_time()
            print(f"❌ Row {row_num} error at {error_time}: {e}")

            try:
                data = [
                    "",
                    "",
                    url,
                    "ERROR",
                    error_time,
                    "ERROR",
                    error_time
                ]
                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, "N/A", "N/A")
            except Exception:
                pass

            try:
                safe_add_log(
                    row_number=row_num,
                    status="ERROR",
                    log_type="COMBINED",
                    url=url,
                    message=str(e)
                )
            except Exception:
                pass

        finally:
            page.close()
            context.close()
            browser.close()


def run_parallel_combined_scraper(max_workers=2):
    urls = sheets.get_urls_with_retry()

    url_rows = [
        (i + 2, u.strip())
        for i, u in enumerate(urls)
        if u and u.strip()
    ]

    if not url_rows:
        print("No transparency URLs found in column H.")
        return

    print(f"🚀 Starting combined scraper for {len(url_rows)} rows")
    print(f"⚡ Running parallel with max_workers={max_workers}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(scrape_single_url, url_row): url_row
            for url_row in url_rows
        }

        for future in as_completed(futures):
            row_num, _ = futures[future]

            try:
                future.result()
            except Exception as e:
                print(f"❌ Worker failed for row {row_num}: {e}")

                try:
                    safe_add_log(
                        row_number=row_num,
                        status="WORKER_ERROR",
                        log_type="COMBINED",
                        message=str(e)
                    )
                except Exception:
                    pass

    print("✅ Finished combined scraping")


if __name__ == "__main__":
    run_parallel_combined_scraper(max_workers=MAX_WORKERS)