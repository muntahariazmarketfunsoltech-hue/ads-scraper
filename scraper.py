# Combined Google Ads Transparency scraper
# STABLE VERSION: Single unified package matching logic
# Video-ad detection: original working logic preserved
# Non-video ads: text/image extraction + strict package matching at 0.76

from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import difflib
import re
import time
import threading
import sheets

# ========================================
# CONFIGURATION - FROZEN CONSTANTS
# ========================================
MAX_WORKERS = 2
SHEET_LOCK = threading.Lock()
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v", ".m3u8")

# PACKAGE MATCHING THRESHOLDS - FROZEN (DO NOT MODIFY)
MIN_PACKAGE_MATCH_SCORE = 0.76
SCORE_DIRECT_CORE = 0.98      # Package core directly found in text (6+ chars)
SCORE_TWO_EXACT_HITS = 0.92   # Two or more exact token hits
SCORE_ONE_LONG_EXACT = 0.80   # One exact hit with 8+ characters
SCORE_TWO_PARTIAL_HITS = 0.76 # Two or more partial token hits
SCORE_FUZZY_CORE = 0.82       # Fuzzy match on core (8+ chars, 88%+ similar)

INSTALL_SELECTORS = [
    "a.install-button-anchor.svg-anchor",
    "a.install-button-anchor",
    'a[data-asoch-targets-ad-objective-type]',
    'a:has-text("Install")',
    'a:has-text("Get")',
    'a:has-text("Download")',
]

_GENERIC_PACKAGE_TOKENS = {
    "com", "net", "org", "co", "io", "app", "apps", "android", "mobile",
    "google", "play", "store", "free", "pro", "lite", "online", "official",
    "inc", "ltd", "llc", "studio", "studios", "company", "group", "digital",
    "ai", "all", "new", "best", "easy", "fast"
}

# ========================================
# THREAD-SAFE UTILITIES
# ========================================

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

# ========================================
# PACKAGE EXTRACTION FROM APP LINKS
# ========================================

def extract_package_name(app_link):
    """Extracts package name from app store link."""
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

# ========================================
# VIDEO ID EXTRACTION (ORIGINAL LOGIC)
# ========================================

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
            if any(x in u_lower for x in ["videoplayback", ".mp4", ".webm", ".mov", ".m4v", ".m3u8", "youtube.com/embed/", "youtube.com/watch", "youtu.be/"]):
                video_id = extract_video_id_from_url(u)
                if video_id:
                    return video_id
    except Exception:
        pass
    return "N/A"

def click_possible_video_targets(page):
    selectors = ["video", "iframe", "creative-preview", 'button[aria-label*="Play"]', 'button[title*="Play"]', 'div[aria-label*="Play"]', 'img[src*="play"]']
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
                    if not box or box["width"] < 120 or box["height"] < 80:
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

# ========================================
# APP LINK EXTRACTION
# ========================================

def clean_googleadservices_link(href):
    if not href:
        return "N/A"
    href = href.strip()
    if href.startswith("//"):
        href = "https:" + href
    try:
        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        for key in ["adurl", "url", "q", "u", "ds_dest_url", "destination"]:
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
    return any(x in href for x in ["googleadservices.com/pagead/aclk", "play.google.com", "apps.apple.com", "itunes.apple.com"])

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
                    if not box or box["width"] < 20 or box["height"] < 10:
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
                    candidates.append({"href": final_href, "score": score, "box": box, "text": text})
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
            const goodLink = href.includes('googleadservices.com/pagead/aclk') || href.includes('play.google.com') || href.includes('apps.apple.com') || href.includes('itunes.apple.com');
            const looksInstall = cls.includes('install-button-anchor') || text.includes('install') || text.includes('get') || text.includes('download') || aria.includes('install');
            const visible = rect.width > 20 && rect.height > 10 && rect.bottom > 0 && rect.right > 0 && rect.top < window.innerHeight && rect.left < window.innerWidth;
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

# ========================================
# HEADLINE AND DESCRIPTION EXTRACTION
# ========================================

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

# ========================================
# UNIFIED STABLE PACKAGE MATCHING (FROZEN)
# ========================================

def clean_text_for_comparison(text):
    if not text or text == "N/A":
        return ""
    return re.sub(r"[^a-z0-9]", "", str(text).lower())

def split_words_for_comparison(text):
    if not text or text == "N/A":
        return []
    return re.findall(r"[a-z0-9]+", str(text).lower())

def package_tokens_for_matching(pkg):
    if not pkg:
        return []
    raw_tokens = re.split(r"[._-]+", pkg.lower())
    tokens = []
    for token in raw_tokens:
        token = re.sub(r"[^a-z0-9]", "", token)
        if not token or token in _GENERIC_PACKAGE_TOKENS:
            continue
        if len(token) < 3 or token.isdigit():
            continue
        tokens.append(token)
    return tokens

def score_package_against_text(pkg, headline, description):
    """
    UNIFIED STABLE MATCHING - FROZEN THRESHOLDS
    Compares package with visible headline + description only.
    Returns score 0.0 to 1.0 based on frozen constants.
    """
    visible_raw = f"{headline or ''} {description or ''}"
    visible_clean = clean_text_for_comparison(visible_raw)
    visible_words = split_words_for_comparison(visible_raw)
    visible_word_set = set(visible_words)

    if not visible_clean or not visible_words:
        return 0.0

    tokens = package_tokens_for_matching(pkg)
    if not tokens:
        return 0.0

    package_core = "".join(tokens)
    score = 0.0

    # RULE 1: Direct core match (6+ chars found directly)
    if package_core and len(package_core) >= 6 and package_core in visible_clean:
        return SCORE_DIRECT_CORE  # 0.98

    # RULE 2: Count exact and partial hits
    exact_hits = []
    partial_hits = []

    for token in tokens:
        if token in visible_word_set:
            exact_hits.append(token)
            continue
        if len(token) >= 6 and token in visible_clean:
            exact_hits.append(token)
            continue
        for word in visible_words:
            if len(token) >= 5 and len(word) >= 5 and (token in word or word in token):
                partial_hits.append(token)
                break

    exact_hits = list(dict.fromkeys(exact_hits))
    partial_hits = list(dict.fromkeys(partial_hits))
    total_hits = len(set(exact_hits + partial_hits))

    # RULE 3: Two+ exact hits
    if len(exact_hits) >= 2:
        return SCORE_TWO_EXACT_HITS  # 0.92

    # RULE 4: One exact hit (8+ chars)
    if len(exact_hits) == 1 and len(exact_hits[0]) >= 8:
        return SCORE_ONE_LONG_EXACT  # 0.80

    # RULE 5: Two+ partial hits
    if total_hits >= 2:
        return SCORE_TWO_PARTIAL_HITS  # 0.76

    # RULE 6: Fuzzy core (8+ chars, 88%+ similar)
    if package_core and len(package_core) >= 8:
        core_ratio = difflib.SequenceMatcher(None, visible_clean, package_core).ratio()
        if core_ratio >= 0.88:
            return SCORE_FUZZY_CORE  # 0.82

    return 0.0

def get_best_matching_package(headline, description, package_list):
    """
    UNIFIED matching function - FROZEN threshold at 0.76
    Returns (package, score). If score < 0.76, returns (None, best_score).
    """
    if not package_list:
        return None, 0.0

    best_pkg = None
    best_score = 0.0

    for pkg in sorted(package_list):
        score = score_package_against_text(pkg, headline, description)
        if score > best_score:
            best_score = score
            best_pkg = pkg

    if best_pkg and best_score >= MIN_PACKAGE_MATCH_SCORE:
        return best_pkg, best_score

    return None, best_score

# ========================================
# PACKAGE EXTRACTION FROM PAGE
# ========================================

def decode_all(text):
    text = re.sub(r'\\x3[Dd]', '=', text)
    text = re.sub(r'\\x26', '&', text)
    text = re.sub(r'\\x3[Ff]', '?', text)
    text = re.sub(r'\\x2[Ff]', '/', text)
    text = re.sub(r'\\u003[Dd]', '=', text)
    text = re.sub(r'\\u0026', '&', text)
    text = re.sub(r'\\u003[Ff]', '?', text)
    text = re.sub(r'%3[Dd]', '=', text, flags=re.I)
    text = re.sub(r'%26', '&', text, flags=re.I)
    text = re.sub(r'%3[Ff]', '?', text, flags=re.I)
    text = re.sub(r'%2[Ff]', '/', text, flags=re.I)
    text = re.sub(r'%3[Aa]', ':', text, flags=re.I)
    text = text.replace('&amp;', '&').replace('&quot;', '"').replace('&#38;', '&').replace('&#61;', '=').replace('&#x3D;', '=').replace('&#x26;', '&')
    return text

_SKIP_EXT = re.compile(r'\.(jpg|jpeg|png|gif|webp|svg|ico|css|js|json|xml|html|htm|woff|woff2|ttf|otf|eot|pdf|zip|apk|mp4|mp3|ogg|m3u8)$', re.I)
_SKIP_PFX = re.compile(r'^(com\.google\.android\.(gms|vending|inputmethod|tts|webview)|com\.android\.|android\.|androidx\.|kotlin\.|kotlinx\.|com\.squareup\.|io\.reactivex\.|okhttp3\.|javax\.|java\.|org\.json\.|org\.apache\.)', re.I)

def _is_valid_pkg(pkg):
    parts = pkg.split('.')
    if len(parts) < 3 or len(pkg) < 8:
        return False
    if _SKIP_EXT.search(pkg):
        return False
    if _SKIP_PFX.match(pkg):
        return False
    for p in parts:
        if not p or not re.match(r'^[A-Za-z][A-Za-z0-9_]*$', p):
            return False
    return True

def extract_packages_from_text(raw_text):
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

def extract_advertiser_from_page(page):
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
        for (let el of document.querySelectorAll('body *')) {
            if (el.childElementCount > 0) continue;
            let txt = (el.innerText || "").trim();
            let lower = txt.toLowerCase();
            if (txt.length < 2 || txt.length > 60 || badWords.some(b => lower.includes(b))) continue;
            let rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0 || rect.y < 0 || rect.y > 350 || rect.width < 10) continue;
            let style = window.getComputedStyle(el);
            if (style.opacity === '0' || style.display === 'none' || style.visibility === 'hidden') continue;
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

def has_visible_image_creative(page):
    js = r"""
    () => {
        const isVisible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return (rect.width >= 120 && rect.height >= 80 && rect.bottom > 0 && rect.right > 0 && rect.top < window.innerHeight && rect.left < window.innerWidth && style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0');
        };
        const imageLike = Array.from(document.querySelectorAll('img, picture, canvas, svg')).some(el => {
            const src = String(el.getAttribute('src') || '').toLowerCase();
            const alt = String(el.getAttribute('alt') || '').toLowerCase();
            if (src.includes('googlelogo') || alt.includes('google')) return false;
            return isVisible(el);
        });
        if (imageLike) return true;
        return Array.from(document.querySelectorAll('*')).some(el => {
            if (!isVisible(el)) return false;
            const bg = window.getComputedStyle(el).backgroundImage || '';
            return bg && bg !== 'none' && bg.includes('url(');
        });
    }
    """
    try:
        if page.evaluate(js):
            return True
    except Exception:
        pass
    for frame in page.frames:
        try:
            if frame.evaluate(js):
                return True
        except Exception:
            continue
    return False

def is_valid_text_ad(headline, description):
    if headline and headline != "N/A" and len(clean_text(headline)) >= 3:
        return True
    if description and description != "N/A" and len(clean_text(description)) >= 15:
        return True
    return False

# ========================================
# MAIN COMBINED SCRAPER
# ========================================

def scrape_single_url(url_row):
    row_num, url = url_row

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage", "--disable-web-security"]
        )

        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            service_workers="block",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )

        page = context.new_page()
        captured = {"video_id": "N/A"}

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

            safe_add_log(row_number=row_num, status="STARTED", log_type="COMBINED", url=url, message="Started combined video/text/image ad extraction")

            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)

            advertiser = extract_advertiser_from_page(page)

            # VIDEO DETECTION
            video_id = detect_video_id(page, captured)
            video_time = get_exact_time()

            # =========================
            # VIDEO AD PATH
            # =========================
            if video_id != "N/A":
                print(f"🎬 Row {row_num}: video ID found: {video_id}")

                app_link = wait_and_extract_install_link(page, max_wait_seconds=35)
                app_link_time = get_exact_time()

                headline, description = wait_and_extract_headline_description(page, max_wait_seconds=15)

                if app_link == "N/A":
                    status = "VIDEO_FOUND_APP_LINK_NOT_FOUND"
                    message = "Video ID found, but exact visible install link not found"
                else:
                    status = "SUCCESS"
                    message = "Video ID and app link saved"

                package_name = extract_package_name(app_link)

                data = [advertiser, package_name, url, app_link, app_link_time, video_id, video_time]

                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, headline, description)

                safe_add_log(row_number=row_num, status=status, log_type="VIDEO_AD", url=url, video_id=video_id, app_link=app_link, message=message)

                print(f"✅ Row {row_num}: saved VIDEO ad")
                return

            # =========================
            # NON-VIDEO PATH
            # =========================
            print(f"📄 Row {row_num}: no video, checking text/image ad")

            text_data = wait_and_extract_headline_description(page, max_wait_seconds=15)
            headline = clean_text(text_data.get("headline"))
            description = clean_text(text_data.get("description"))
            process_time = get_exact_time()
            has_text = is_valid_text_ad(headline, description)

            visible_app_link = wait_and_extract_install_link(page, max_wait_seconds=8)
            visible_package = extract_package_name(visible_app_link)

            is_image_like = has_visible_image_creative(page)
            ad_type = "text" if has_text else "image" if (is_image_like or visible_package != "N/A") else "N/A"

            if not has_text and visible_package == "N/A" and not is_image_like:
                data = [advertiser, "N/A", url, "N/A", process_time, "N/A", process_time]
                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, "N/A", "N/A")
                safe_add_log(row_number=row_num, status="NO_VIDEO_NO_TEXT_IMAGE", log_type="COMBINED", url=url, video_id="N/A", app_link="N/A", message="No video ID and no valid text/image creative found")
                print(f"⏭ Row {row_num}: no video and no valid text/image ad found")
                return

            if has_text:
                print(f"🔎 Row {row_num}: text headline detected")

            # PACKAGE RESOLUTION WITH UNIFIED STABLE MATCHING
            if visible_package != "N/A":
                package_name = visible_package
                app_link = visible_app_link
                match_score = 1.0
                status = "SUCCESS"
                message = f"Non-video {ad_type} ad package from visible install link"
                print(f"✅ Row {row_num}: package from visible link -> {package_name}")
            else:
                package_name = None
                match_score = 0.0

                if has_text:
                    print(f"📦 Row {row_num}: STABLE matching headline+description (threshold={MIN_PACKAGE_MATCH_SCORE})")
                    all_found_packages = extract_package_from_page(page)
                    package_name, match_score = get_best_matching_package(headline, description, all_found_packages)

                if package_name:
                    app_link = f"https://play.google.com/store/apps/details?id={package_name}"
                    status = "SUCCESS"
                    message = f"Non-video {ad_type} matched with STABLE score {match_score}"
                    print(f"✅ Row {row_num}: STABLE matched package -> {package_name} | score={match_score}")
                else:
                    package_name = "N/A"
                    app_link = "N/A"
                    status = "NON_VIDEO_PACKAGE_NOT_FOUND"
                    message = f"Non-video {ad_type} package score {match_score} < {MIN_PACKAGE_MATCH_SCORE}"
                    print(f"⚠️ Row {row_num}: score {match_score} below threshold")

            data = [advertiser, package_name, url, app_link, process_time, ad_type, process_time]
            safe_update_combined_row(row_num, data)
            safe_update_headline_desc(row_num, headline if has_text else "N/A", description if has_text else "N/A")
            safe_add_log(row_number=row_num, status=status, log_type="NON_VIDEO_AD", url=url, video_id=ad_type, app_link=app_link, message=message)

            print(f"✅ Row {row_num}: saved NON-VIDEO {ad_type} ad")

        except Exception as e:
            error_time = get_exact_time()
            print(f"❌ Row {row_num} error at {error_time}: {e}")

            try:
                data = ["", "N/A", url, "ERROR", error_time, "ERROR", error_time]
                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, "N/A", "N/A")
            except Exception:
                pass

            try:
                safe_add_log(row_number=row_num, status="ERROR", log_type="COMBINED", url=url, message=str(e))
            except Exception:
                pass

        finally:
            page.close()
            context.close()
            browser.close()

def run_parallel_combined_scraper(max_workers=MAX_WORKERS):
    urls = sheets.get_urls_with_retry()

    url_rows = [(i + 2, u.strip()) for i, u in enumerate(urls) if u and u.strip()]

    if not url_rows:
        print("No transparency URLs found in column H.")
        return

    print(f"🚀 Starting combined VIDEO + TEXT scraper for {len(url_rows)} rows")
    print(f"⚡ Running parallel with max_workers={max_workers}")
    print(f"🔒 FROZEN THRESHOLDS:")
    print(f"   MIN_PACKAGE_MATCH_SCORE: {MIN_PACKAGE_MATCH_SCORE}")
    print(f"   SCORE_DIRECT_CORE: {SCORE_DIRECT_CORE}")
    print(f"   SCORE_TWO_EXACT_HITS: {SCORE_TWO_EXACT_HITS}")
    print(f"   SCORE_ONE_LONG_EXACT: {SCORE_ONE_LONG_EXACT}")
    print(f"   SCORE_TWO_PARTIAL_HITS: {SCORE_TWO_PARTIAL_HITS}")
    print(f"   SCORE_FUZZY_CORE: {SCORE_FUZZY_CORE}")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(scrape_single_url, url_row): url_row for url_row in url_rows}

        for future in as_completed(futures):
            row_num, _ = futures[future]
            try:
                future.result()
            except Exception as e:
                print(f"❌ Worker failed for row {row_num}: {e}")
                try:
                    safe_add_log(row_number=row_num, status="WORKER_ERROR", log_type="COMBINED", message=str(e))
                except Exception:
                    pass

    print("✅ Finished combined video + text scraping")


if __name__ == "__main__":
    run_parallel_combined_scraper(max_workers=MAX_WORKERS)
