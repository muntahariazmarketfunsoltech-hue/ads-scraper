from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import difflib
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


# =========================
# SHEET WRITING HELPERS
# =========================

def safe_update_combined_row(row_num, data):
    """Thread-safe Google Sheet row update."""
    with SHEET_LOCK:
        sheets.update_combined_row(row_num, data)


def safe_update_headline_desc(row_num, headline, description):
    """Thread-safe Google Sheet row update."""
    with SHEET_LOCK:
        sheets.update_headline_and_description(row_num, headline, description)


def safe_add_log(row_number, status, log_type, url="", video_id="", app_link="", message=""):
    """Thread-safe log writing."""
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
# PACKAGE EXTRACTION (TEXT ADS)
# =========================

def decode_all(text):
    """Decode every encoding variant."""
    text = re.sub(r'\\x3[Dd]', '=', text)
    text = re.sub(r'\\x26',    '&', text)
    text = re.sub(r'\\x3[Ff]', '?', text)
    text = re.sub(r'\\x2[Ff]', '/', text)
    text = re.sub(r'\\u003[Dd]', '=', text)
    text = re.sub(r'\\u0026',    '&', text)
    text = re.sub(r'\\u003[Ff]', '?', text)
    text = re.sub(r'%3[Dd]', '=', text, flags=re.I)
    text = re.sub(r'%26',    '&', text, flags=re.I)
    text = re.sub(r'%3[Ff]', '?', text, flags=re.I)
    text = re.sub(r'%2[Ff]', '/', text, flags=re.I)
    text = re.sub(r'%3[Aa]', ':', text, flags=re.I)
    text = (text.replace('&amp;', '&').replace('&quot;', '"')
                .replace('&#38;', '&').replace('&#61;', '=')
                .replace('&#x3D;', '=').replace('&#x26;', '&'))
    return text


_SKIP_EXT = re.compile(
    r'\.(jpg|jpeg|png|gif|webp|svg|ico|css|js|json|xml|html|htm|'
    r'woff|woff2|ttf|otf|eot|pdf|zip|apk|mp4|mp3|ogg|m3u8)$', re.I)
_SKIP_PFX = re.compile(
    r'^(com\.google\.android\.(gms|vending|inputmethod|tts|webview)|'
    r'com\.android\.|android\.|androidx\.|kotlin\.|kotlinx\.|'
    r'com\.squareup\.|io\.reactivex\.|okhttp3\.|javax\.|java\.|'
    r'org\.json\.|org\.apache\.)', re.I)


def _is_valid_pkg(pkg):
    parts = pkg.split('.')
    if len(parts) < 3 or len(pkg) < 8:  return False
    if _SKIP_EXT.search(pkg):            return False
    if _SKIP_PFX.match(pkg):             return False
    for p in parts:
        if not p or not re.match(r'^[A-Za-z][A-Za-z0-9_]*$', p):
            return False
    return True


def extract_packages_from_text(raw_text):
    """Returns a SET of all unique, valid package names."""
    text = decode_all(raw_text)
    candidates = set()   

    patterns = [
        r"""['"]appId['"]\s*:\s*['"]([A-Za-z][\w.]+)['"]""",
        r"""play\.google\.com/store/apps/details[^\s'"<>]*[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})""",
        r"""market://[^\s'"]*[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})""",
        r"""(?:destination_url|final_url|click_url|destUrl|clickUrl|landingUrl)['"\s]*:['"\s]*['"][^'"]*[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})""",
        r"""[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})""",
        r"""[?&]package=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){2,})"""
    ]

    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            pkg = m.group(1).rstrip('.,;\'"\\ ')
            if _is_valid_pkg(pkg):
                candidates.add(pkg)

    return candidates


def extract_package_from_page(page):
    """Scans the DOM and visible links for packages."""
    collected_texts = []

    for frame in page.frames:
        try:
            frame_html = frame.evaluate("() => document.documentElement.outerHTML")
            if frame_html and len(frame_html) > 200:
                collected_texts.append(frame_html)

            hrefs = frame.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]'))
                           .map(a => a.href).filter(Boolean)
            """)
            if hrefs:
                collected_texts.append('\n'.join(hrefs))

            visible = frame.evaluate("() => document.body ? document.body.innerText : ''")
            if visible:
                collected_texts.append(visible)

        except Exception:
            continue

    try:
        visible = page.evaluate("() => document.body ? document.body.innerText : ''")
        if visible:
            collected_texts.append(visible)
        
        hrefs = page.evaluate("""
            () => Array.from(document.querySelectorAll('a[href]'))
                       .map(a => a.href).filter(Boolean)
        """)
        if hrefs:
            collected_texts.append('\n'.join(hrefs))
            
        main_html = page.evaluate("() => document.documentElement.outerHTML")
        if main_html:
            collected_texts.append(main_html)
    except Exception:
        pass

    combined = '\n'.join(collected_texts)
    return extract_packages_from_text(combined)


def clean_text_for_comparison(text):
    """Strips spaces and punctuation for comparison."""
    if not text or text == "N/A": return ""
    return re.sub(r'[^a-z0-9]', '', str(text).lower())


def get_best_matching_package(headline, advertiser, package_list):
    """Match headline to package with 90% threshold."""
    if not package_list: 
        return None
    
    if len(package_list) == 1: 
        return list(package_list)[0]

    best_pkg = None
    highest_ratio = 0.0
    THRESHOLD = 0.90

    visible_target = clean_text_for_comparison(f"{headline}{advertiser}")

    for pkg in package_list:
        clean_pkg = re.sub(r'^(com\.|net\.|org\.|android\.)', '', pkg.lower())
        clean_pkg = re.sub(r'[^a-z0-9]', '', clean_pkg)
        ratio = difflib.SequenceMatcher(None, visible_target, clean_pkg).ratio()
        
        if ratio > highest_ratio:
            highest_ratio = ratio
            best_pkg = pkg if ratio >= THRESHOLD else None

    return best_pkg if highest_ratio >= THRESHOLD else None


def extract_package_name(app_link):
    """Extract package from app link."""
    if not app_link or app_link == "N/A":
        return "N/A"
    
    try:
        if "play.google.com" in app_link.lower():
            parsed = urlparse(app_link)
            query = parse_qs(parsed.query)
            package_name = query.get("id", [None])[0]
            if package_name:
                return package_name
        
        if "apps.apple.com" in app_link.lower():
            match = re.search(r"/id(\d+)", app_link)
            if match:
                return f"id{match.group(1)}"
        
        return "N/A"
    
    except Exception:
        return "N/A"


# =========================
# VIDEO ID EXTRACTION (ORIGINAL LOGIC)
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
    """Extracts clean video IDs from URLs."""
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
    """Check for video elements in DOM."""
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
    """Scan performance entries for videos."""
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
    """Click video play buttons with bounding box checks."""
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
    """ORIGINAL: Poll for video ID over time."""
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
    """
    ORIGINAL DETECTION LOGIC: 
    1. Check DOM for video
    2. Click and wait
    3. Check performance API
    4. Scroll and retry
    """
    video_id = extract_video_from_dom(page)

    if video_id == "N/A":
        click_possible_video_targets(page)
        video_id = wait_for_video_id(page, captured, max_seconds=15)

    if video_id == "N/A":
        video_id = scan_browser_performance_for_video(page)

    if video_id == "N/A":
        page.mouse.wheel(0, 400)  # ← SCROLL - IMPORTANT!
        page.wait_for_timeout(1500)

        click_possible_video_targets(page)
        video_id = wait_for_video_id(page, captured, max_seconds=10)

    return video_id


# =========================
# APP LINK EXTRACTION (ORIGINAL LOGIC)
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

        possible_keys = [
            "adurl",
            "url",
            "q",
            "u",
            "ds_dest_url",
            "destination",
        ]

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
    """ORIGINAL: Score install button candidates."""
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
    """ORIGINAL: Extract visible install button."""
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
    """ORIGINAL: Precise JS fallback."""
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

            if (!goodLink || !looksInstall || !visible) {
                return null;
            }

            let score = 0;
            if (cls.includes('install-button-anchor')) score += 100;
            if (text.includes('install')) score += 80;
            if (text.includes('get') || text.includes('download')) score += 40;
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;
            if (cx >= 350 && cx <= 850) score += 40;
            if (cy >= 50 && cy <= 700) score += 40;
            if (cy > 700) score -= 100;
            return {
                href,
                score
            };
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
    """ORIGINAL: Wait and extract with retries."""
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
# TEXT AD EXTRACTION
# =========================

def wait_and_extract_headline_description(page, max_wait_seconds=15):
    """Extract headline and description."""
    js = r"""
    () => {
        let result = { headline: "N/A", description: "N/A" };
        const isBadText = (txt) => {
            const lower = txt.toLowerCase();
            const exactBlock = ['install', 'download', 'get', 'open', 'visit site', 'learn more', 'sign in', 'google', 'search', 'ad details', 'ads transparency'];
            if (exactBlock.includes(lower)) return true;
            if (lower.length < 15 && (lower.startsWith('install') || lower.startsWith('download') || lower.startsWith('get '))) return true;
            return false;
        };
        
        let maxFont = 0;
        let bestEl = null;
        for (let el of document.querySelectorAll('*')) {
            if (el.childElementCount > 0) continue;
            let txt = (el.innerText || "").trim();
            if (txt.length < 4 || isBadText(txt)) continue;
            
            let rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;

            let style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
            
            let fontSize = parseFloat(style.fontSize || '0');
            if (fontSize > maxFont) {
                maxFont = fontSize;
                bestEl = el;
            }
        }

        if (bestEl) {
            result.headline = bestEl.innerText.replace(/\n/g, ' ').trim();
            
            let maxLen = 0;
            for (let el of document.querySelectorAll('*')) {
                if (el.childElementCount > 0) continue;
                let txt = (el.innerText || "").replace(/\n/g, ' ').trim();
                if (txt === result.headline || txt.length < 15 || isBadText(txt)) continue;
                
                let rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) continue;

                let style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                
                if (txt.length > maxLen) {
                    maxLen = txt.length;
                    result.description = txt;
                }
            }
        }
        return result;
    }
    """

    start_time = time.time()

    while time.time() - start_time < max_wait_seconds:
        try:
            data = page.evaluate(js)
            if data["headline"] != "N/A":
                return data["headline"], data["description"]
        except Exception:
            pass

        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                data = frame.evaluate(js)
                if data["headline"] != "N/A":
                    return data["headline"], data["description"]
            except Exception:
                continue
        
        page.wait_for_timeout(1000)

    return "N/A", "N/A"


def extract_advertiser_from_page(page):
    """Extract advertiser from page header."""
    try:
        loc = page.locator('.advertiser-title, [data-test-id="advertiser-name"]').first
        loc.wait_for(timeout=4000)
        text = loc.inner_text().strip()
        if text and len(text) > 1 and "Sign in" not in text:
            return text
    except Exception:
        pass

    js = r"""
    () => {
        const badWords = ['sign in', 'log in', 'home', 'menu', 'search', 'help', 'privacy', 'terms', 'ad details', 'see more ads', 'ads transparency'];
        let maxFont = 0;
        let advertiserName = "N/A";

        for (let el of document.querySelectorAll('*')) {
            if (el.childElementCount > 0) continue;
            
            let txt = (el.innerText || "").trim();
            if (!txt || txt.length < 2) continue;
            
            let lower = txt.toLowerCase();
            if (badWords.includes(lower)) continue;
            
            let rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;
            
            let style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
            
            let font = parseFloat(style.fontSize || '0');
            if (font > maxFont) {
                maxFont = font;
                advertiserName = txt;
            }
        }
        return advertiserName;
    }
    """
    
    try:
        if advertiser := page.evaluate(js): 
            return advertiser
    except Exception:
        pass
        
    return "N/A"


# =========================
# MAIN SCRAPER - PROPERLY UNIFIED
# =========================

def scrape_single_url(url_row):
    """
    UNIFIED SCRAPER (Fixed - Using Original Logic)
    
    1. Try to detect VIDEO using original logic (with scrolling!)
    2. IF video found → extract app link using original logic
    3. IF no video → extract text ad using text extraction
    4. Save appropriate data to sheet
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
        captured = {"video_id": "N/A"}

        # Network response handler for video capture
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
                message="Started scraping"
            )

            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)

            # Step 1: Extract advertiser
            advertiser = extract_advertiser_from_page(page)

            # Step 2: Detect video using ORIGINAL logic
            print(f"🎬 Row {row_num}: detecting video...")
            video_id = detect_video_id(page, captured)
            video_time = get_exact_time()

            if video_id == "N/A":
                # ════════════════════════════════════════════════════════════
                # TEXT AD PATH - No video detected
                # ════════════════════════════════════════════════════════════
                
                print(f"📄 Row {row_num}: TEXT AD (no video)")
                text_time = get_exact_time()

                # Extract headline and description
                headline, description = wait_and_extract_headline_description(page, max_wait_seconds=15)

                if headline == "N/A" or len(headline) < 3:
                    print(f"⏭ Row {row_num}: NO VALID TEXT AD HEADLINE - SKIP")
                    
                    safe_add_log(
                        row_number=row_num,
                        status="NO_VALID_TEXT",
                        log_type="COMBINED",
                        url=url,
                        message="No valid text ad headline"
                    )
                    return

                # Extract packages and match
                all_packages = extract_package_from_page(page)
                package_name = get_best_matching_package(headline, advertiser, all_packages)

                if package_name:
                    app_link = f"https://play.google.com/store/apps/details?id={package_name}"
                    status = "SUCCESS"
                else:
                    app_link = "N/A"
                    package_name = "NOT FOUND"
                    status = "TEXT_AD_NO_MATCH"

                # Save TEXT AD data
                data = [
                    advertiser,
                    package_name,
                    url,
                    app_link,
                    text_time,
                    "TEXT_AD",  # ← Show "TEXT_AD" in video ID column
                    text_time
                ]

                message = f"TEXT_AD | Package: {package_name} | Headline: {headline[:40]}"

            else:
                # ════════════════════════════════════════════════════════════
                # VIDEO AD PATH - Video detected
                # ════════════════════════════════════════════════════════════
                
                print(f"🎬 Row {row_num}: VIDEO DETECTED - {video_id}")

                # Extract app link using ORIGINAL logic
                app_link = wait_and_extract_install_link(page, max_wait_seconds=35)
                app_link_time = get_exact_time()

                # Extract headline and description
                headline, description = wait_and_extract_headline_description(page, max_wait_seconds=15)

                # Extract package from app link
                package_name = extract_package_name(app_link)

                if app_link == "N/A":
                    status = "VIDEO_FOUND_APP_LINK_NOT_FOUND"
                    message = "Video ID found, but app link not found"
                else:
                    status = "SUCCESS"
                    message = f"VIDEO_AD | Video: {video_id} | Package: {package_name}"

                # Save VIDEO AD data
                data = [
                    advertiser,
                    package_name,
                    url,
                    app_link,
                    app_link_time,
                    video_id,  # ← Actual video ID
                    video_time
                ]

            # ════════════════════════════════════════════════════════════
            # SAVE TO SHEET
            # ════════════════════════════════════════════════════════════
            
            safe_update_combined_row(row_num, data)
            safe_update_headline_desc(row_num, headline, description)

            safe_add_log(
                row_number=row_num,
                status=status,
                log_type="COMBINED",
                url=url,
                video_id=video_id if video_id != "N/A" else "TEXT_AD",
                app_link=app_link,
                message=message
            )

            print(f"✅ Row {row_num}: SAVED")

        except Exception as e:
            error_time = get_exact_time()

            print(f"❌ Row {row_num} error: {str(e)[:80]}")

            try:
                data = [
                    "",
                    "N/A",
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
                    message=str(e)[:100]
                )
            except Exception:
                pass

        finally:
            page.close()
            context.close()
            browser.close()


def run_parallel_combined_scraper(max_workers=MAX_WORKERS):
    """Run scraper in parallel."""
    urls = sheets.get_urls_with_retry()

    url_rows = [
        (i + 2, u.strip())
        for i, u in enumerate(urls)
        if u and u.strip()
    ]

    if not url_rows:
        print("No URLs found in sheet")
        return

    print(f"🚀 Starting unified scraper for {len(url_rows)} rows")
    print(f"⚡ Running with max_workers={max_workers}")

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
                print(f"❌ Worker failed for row {row_num}: {str(e)[:80]}")

    print("✅ Finished scraping")


if __name__ == "__main__":
    run_parallel_combined_scraper(max_workers=MAX_WORKERS)
