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
    """Thread-safe Google Sheet row update for Headline and Description."""
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
    """Decode every encoding variant so no package name is missed."""
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
    """Returns a SET of all unique, valid package names found in the text."""
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
    """Scans strictly the rendered DOM and visible links."""
    collected_texts = []

    # Scan all frames
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

    # Scan main frame
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
    """Strips spaces, punctuation, and makes text lowercase for comparison."""
    if not text or text == "N/A": return ""
    return re.sub(r'[^a-z0-9]', '', str(text).lower())


def get_best_matching_package(headline, advertiser, package_list):
    """
    Compares visible headline/advertiser against package names.
    Returns the one with highest similarity score (90% threshold).
    """
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


def extract_package_name_from_link(app_link):
    """Extracts package name from Google Play Store link."""
    if not app_link or app_link == "N/A":
        return "N/A"
    
    try:
        if "play.google.com" in app_link.lower():
            parsed = urlparse(app_link)
            query = parse_qs(parsed.query)
            package_name = query.get("id", [None])[0]
            if package_name:
                return package_name
        return "N/A"
    except Exception:
        return "N/A"


# =========================
# VIDEO ID EXTRACTION
# =========================

def is_real_video_response(response):
    """Check if a network response is a real video."""
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
    """Extracts clean video IDs or filenames from URLs."""
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
    """Checks actual video elements on page and inside frames."""
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
    """Scans performance entries for real video URLs only."""
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
    """Clicks possible video preview areas."""
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
                    el.click(timeout=1000)
                except Exception:
                    pass

        except Exception:
            continue


def detect_video_id(page, captured):
    """
    MAIN VIDEO DETECTION - Returns video_id if found, "N/A" if text ad
    Uses 4 methods to detect video:
    1. Network response capture
    2. DOM video elements
    3. Performance API
    4. Click and retry
    """
    print(f"    🔍 Method 1: Checking network captures...")
    if captured.get("video_id") and captured["video_id"] != "N/A":
        print(f"    ✅ FOUND via network response: {captured['video_id']}")
        return captured["video_id"]

    print(f"    🔍 Method 2: Checking DOM video elements...")
    video_id = extract_video_from_dom(page)
    if video_id != "N/A":
        print(f"    ✅ FOUND in DOM: {video_id}")
        return video_id

    print(f"    🔍 Method 3: Checking performance API...")
    video_id = scan_browser_performance_for_video(page)
    if video_id != "N/A":
        print(f"    ✅ FOUND in performance API: {video_id}")
        return video_id

    print(f"    🔍 Method 4: Clicking play buttons and retrying...")
    click_possible_video_targets(page)
    page.wait_for_timeout(2000)

    video_id = scan_browser_performance_for_video(page)
    if video_id != "N/A":
        print(f"    ✅ FOUND after clicking: {video_id}")
        return video_id

    print(f"    ❌ NO VIDEO FOUND - This is a TEXT AD")
    return "N/A"


# =========================
# ADVERTISER EXTRACTION
# =========================

def extract_advertiser_from_page(page):
    """Extracts advertiser name from page."""
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
# TEXT AD EXTRACTION
# =========================

def wait_and_extract_text_ad_details(page, max_wait_seconds=15):
    """Extracts headline and description from text ads."""
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
        
        // 1. EXTRACT HEADLINE
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
            
            // 2. EXTRACT DESCRIPTION
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
        # Try main frame first
        try:
            data = page.evaluate(js)
            if data["headline"] != "N/A":
                return data
        except Exception:
            pass

        # Try iframes
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                data = frame.evaluate(js)
                if data["headline"] != "N/A":
                    return data
            except Exception:
                continue
        
        page.wait_for_timeout(1000)

    return {"headline": "N/A", "description": "N/A"}


# =========================
# INSTALL LINK EXTRACTION
# =========================

def wait_and_extract_install_link(page, max_wait_seconds=35):
    """Extracts install link from page with extensive waiting."""
    start_time = time.time()

    while time.time() - start_time < max_wait_seconds:
        for sel in INSTALL_SELECTORS:
            try:
                elements = page.locator(sel)
                if not elements:
                    continue

                count = elements.count()
                for i in range(count):
                    el = elements.nth(i)

                    if not el.is_visible(timeout=500):
                        continue

                    href = el.get_attribute("href")
                    if not href:
                        continue

                    href = href.strip()

                    if "play.google.com" not in href.lower() and "apps.apple.com" not in href.lower():
                        continue

                    if href != "N/A":
                        return href

            except Exception:
                continue

        page.wait_for_timeout(1000)

    return "N/A"


# =========================
# MAIN SCRAPER - UNIFIED LOGIC (FIXED)
# =========================

def scrape_single_url(url_row):
    """
    UNIFIED SCRAPER WITH PROPER VIDEO/TEXT DISTINCTION
    
    FIXED LOGIC:
    1. Try to detect VIDEO ID using 4 methods
    2. IF video found → Extract install link, get package from link
    3. IF no video (N/A) → Extract packages from page, match to headline
    4. Save actual video_id OR "TEXT_AD" in column F
    5. ALWAYS extract and save headline & description
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

        # ═══════════════════════════════════════════════════════════════
        # RESPONSE HANDLER FOR VIDEO DETECTION
        # ═══════════════════════════════════════════════════════════════

        def handle_response(response):
            try:
                if not is_real_video_response(response):
                    return

                video_id = extract_video_id_from_url(response.url)

                if video_id and captured["video_id"] == "N/A":
                    captured["video_id"] = video_id
                    print(f"    📡 VIDEO CAPTURED FROM NETWORK: {video_id}")

            except Exception:
                pass

        page.on("response", handle_response)

        try:
            # Add region parameter if missing
            if "region=" not in url:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}region=anywhere"

            print(f"\n{'='*80}")
            print(f"🔍 Row {row_num}: Opening URL")
            print(f"   {url}")

            safe_add_log(
                row_number=row_num,
                status="STARTED",
                log_type="SCRAPING",
                url=url,
                message="Started scraping"
            )

            # Load page
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)

            # Step 1: Extract advertiser (common to both)
            advertiser = extract_advertiser_from_page(page)
            print(f"🏷️  Advertiser: {advertiser}")

            # Step 2: DETECT VIDEO ID (THE KEY DECISION POINT)
            print(f"🎬 Attempting to detect VIDEO...")
            print(f"    Testing 4 different detection methods...")
            video_id = detect_video_id(page, captured)
            
            # ═══════════════════════════════════════════════════════════════
            # DECISION: VIDEO OR TEXT AD?
            # ═══════════════════════════════════════════════════════════════
            
            if video_id != "N/A":
                # ╔═══════════════════════════════════════════════════════════╗
                # ║                   VIDEO AD DETECTED                       ║
                # ╚═══════════════════════════════════════════════════════════╝
                
                print(f"✅ VIDEO AD DETECTED")
                print(f"   Video ID: {video_id}")
                video_time = get_exact_time()

                # Extract install link (for video ads)
                print(f"   📦 Extracting install link from page...")
                app_link = wait_and_extract_install_link(page, max_wait_seconds=35)
                
                if app_link != "N/A":
                    print(f"   ✅ Install link found: {app_link[:60]}...")
                    package_name = extract_package_name_from_link(app_link)
                    print(f"   ✅ Package extracted: {package_name}")
                else:
                    print(f"   ⚠️  Install link NOT found")
                    app_link = "N/A"
                    package_name = "N/A"

                # Extract headline and description
                print(f"   📝 Extracting headline & description...")
                headline, description = wait_and_extract_text_ad_details(page, max_wait_seconds=15)
                if headline != "N/A":
                    print(f"   ✅ Headline: {headline[:50]}...")
                if description != "N/A":
                    print(f"   ✅ Description: {description[:50]}...")

                # ═══════════════════════════════════════════════════════════
                # SAVE VIDEO AD DATA
                # ═══════════════════════════════════════════════════════════
                
                data = [
                    advertiser,
                    package_name,
                    url,
                    app_link,
                    video_time,
                    video_id,              # ← ACTUAL VIDEO ID (NOT "TEXT_AD")
                    video_time
                ]

                status = "SUCCESS" if app_link != "N/A" else "VIDEO_FOUND_NO_INSTALL"
                log_msg = f"VIDEO_AD | Video ID: {video_id} | Package: {package_name}"

            else:
                # ╔═══════════════════════════════════════════════════════════╗
                # ║                   TEXT AD DETECTED                        ║
                # ╚═══════════════════════════════════════════════════════════╝
                
                print(f"📄 TEXT AD DETECTED (no video found)")
                text_time = get_exact_time()

                # Extract headline and description
                print(f"   📝 Extracting text ad headline & description...")
                text_data = wait_and_extract_text_ad_details(page, max_wait_seconds=15)
                headline = text_data.get("headline", "N/A")
                description = text_data.get("description", "N/A")

                if headline == "N/A" or len(headline) < 3:
                    print(f"   ⚠️  NO VALID TEXT AD HEADLINE FOUND - SKIPPING")
                    safe_add_log(
                        row_number=row_num,
                        status="NO_VALID_TEXT",
                        log_type="SCRAPING",
                        url=url,
                        message="Text ad headline not found"
                    )
                    print(f"✅ Row {row_num}: SKIPPED (no valid headline)")
                    print(f"{'='*80}\n")
                    return

                print(f"   ✅ Headline: {headline[:50]}...")
                if description != "N/A":
                    print(f"   ✅ Description: {description[:50]}...")

                # Find packages and match to headline
                print(f"   📦 Extracting packages from page...")
                all_packages = extract_package_from_page(page)
                print(f"   📦 Found {len(all_packages)} package candidate(s)")
                
                if all_packages:
                    for pkg in list(all_packages)[:3]:
                        print(f"      - {pkg}")
                
                package_name = get_best_matching_package(headline, advertiser, all_packages)

                if package_name:
                    app_link = f"https://play.google.com/store/apps/details?id={package_name}"
                    print(f"   ✅ Package matched (90% threshold): {package_name}")
                    status = "SUCCESS"
                else:
                    app_link = "N/A"
                    package_name = "NOT FOUND"
                    print(f"   ⚠️  No package match found at 90% threshold")
                    status = "TEXT_AD_NO_MATCH"

                # ═══════════════════════════════════════════════════════════
                # SAVE TEXT AD DATA
                # ═══════════════════════════════════════════════════════════
                
                data = [
                    advertiser,
                    package_name,
                    url,
                    app_link,
                    text_time,
                    "TEXT_AD",              # ← SHOW "TEXT_AD" (NOT A VIDEO ID)
                    text_time
                ]

                log_msg = f"TEXT_AD | Package: {package_name} | Headline: {headline[:40]}"

            # ═══════════════════════════════════════════════════════════════
            # SAVE RESULTS (BOTH VIDEO AND TEXT ADS)
            # ═══════════════════════════════════════════════════════════════
            
            print(f"   💾 Saving to Google Sheet...")
            safe_update_combined_row(row_num, data)
            safe_update_headline_desc(row_num, headline, description)

            safe_add_log(
                row_number=row_num,
                status=status,
                log_type="SCRAPING",
                url=url,
                video_id=video_id if video_id != "N/A" else "TEXT_AD",
                app_link=app_link,
                message=log_msg
            )

            print(f"✅ Row {row_num}: SAVED")
            print(f"{'='*80}\n")

        except Exception as e:
            error_time = get_exact_time()
            error_msg = str(e)[:150]

            print(f"❌ Row {row_num} ERROR: {error_msg}")
            print(f"{'='*80}\n")

            try:
                data = ["", "N/A", url, "ERROR", error_time, "ERROR", error_time]
                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, "N/A", "N/A")
            except Exception:
                pass

            try:
                safe_add_log(
                    row_number=row_num,
                    status="ERROR",
                    log_type="SCRAPING",
                    url=url,
                    message=error_msg
                )
            except Exception:
                pass

        finally:
            page.close()
            context.close()
            browser.close()


def run_parallel_scraper(max_workers=MAX_WORKERS):
    """Run the unified scraper in parallel."""
    urls = sheets.get_urls_with_retry()

    url_rows = [
        (i + 2, u.strip())
        for i, u in enumerate(urls)
        if u and u.strip()
    ]

    if not url_rows:
        print("❌ No URLs found in sheet")
        return

    print(f"\n{'='*80}")
    print(f"🚀 UNIFIED VIDEO + TEXT AD SCRAPER (FIXED)")
    print(f"   Total URLs to scrape: {len(url_rows)}")
    print(f"   Max workers: {max_workers}")
    print(f"{'='*80}\n")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(scrape_single_url, url_row): url_row
            for url_row in url_rows
        }

        completed = 0
        for future in as_completed(futures):
            row_num, _ = futures[future]
            completed += 1

            try:
                future.result()
            except Exception as e:
                print(f"❌ Worker failed for row {row_num}: {str(e)[:80]}")

            print(f"📊 Progress: {completed}/{len(url_rows)}")

    print(f"\n{'='*80}")
    print(f"✅ SCRAPING COMPLETE - All {len(url_rows)} URLs processed")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    run_parallel_scraper(max_workers=MAX_WORKERS)
