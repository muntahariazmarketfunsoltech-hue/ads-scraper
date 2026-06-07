"""
UNIFIED AD SCRAPER v3.1
Extracts IMAGE and TEXT ads with intelligent type detection and dynamic DOM scanning.
Skips video processing for faster execution.
"""

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

# =========================
# SHEET OPERATIONS (Thread-Safe)
# =========================

def safe_update_combined_row(row_num, data):
    with SHEET_LOCK:
        sheets.update_combined_row(row_num, data)

def safe_update_headline_desc(row_num, headline, description):
    with SHEET_LOCK:
        sheets.update_headline_and_description(row_num, headline, description)

def safe_add_log(row_number, status, log_type, url="", video_id="", app_link="", message=""):
    with SHEET_LOCK:
        sheets.add_log(
            row_number=row_number, status=status, log_type=log_type,
            url=url, video_id=video_id, app_link=app_link, message=message
        )

def get_exact_time():
    return datetime.now().strftime("%I:%M:%S %p")

def clean_text(value):
    if not value:
        return "N/A"
    return re.sub(r"\s+", " ", str(value)).strip() or "N/A"


# =========================
# PACKAGE EXTRACTION LOGIC
# =========================

def decode_all(text, passes=3):
    """Multi-pass URL decoding"""
    for _ in range(passes):
        prev = text
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
        text = (text.replace('&amp;', '&').replace('&quot;', '"')
                    .replace('&#38;', '&').replace('&#61;', '=')
                    .replace('&#x3D;', '=').replace('&#x26;', '&'))
        if text == prev:
            break
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
    if len(parts) < 2 or len(pkg) < 5:
        return False
    if _SKIP_EXT.search(pkg) or _SKIP_PFX.match(pkg):
        return False
    for p in parts:
        if not p or not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', p):
            return False
    return True

def extract_packages_from_text(raw_text):
    """Extract packages with multiple patterns"""
    text = decode_all(raw_text)
    candidates = set()
    patterns = [
        r"""['"](?:appId|bundleId|appPackage|applicationId|packageName)['"]\s*:\s*['"]([A-Za-z][\w.]+)['"]""",
        r"""play\.google\.com/store/apps/details[^\s'"<>]*[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){1,})""",
        r"""market://[^\s'"]*[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){1,})""",
        r"""[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){1,})""",
        r"""[?&]package=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){1,})"""
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            pkg = m.group(1).rstrip('.,;\'"\\ ')
            if _is_valid_pkg(pkg):
                candidates.add(pkg)
    return candidates

def extract_package_from_page(page):
    """Scan page for packages"""
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
    if not text or text == "N/A":
        return ""
    return re.sub(r'[^a-z0-9]', '', str(text).lower())

def get_best_matching_package(headline, advertiser, package_list):
    """Match package to headline using flexible substring scoring"""
    if not package_list:
        return None
    if len(package_list) == 1:
        return list(package_list)[0]

    best_pkg = None
    highest_score = 0.0

    head_clean = clean_text_for_comparison(headline)
    adv_clean = clean_text_for_comparison(advertiser)

    for pkg in package_list:
        clean_pkg = re.sub(r'^(com\.|net\.|org\.|android\.|io\.)', '', pkg.lower())
        clean_pkg = re.sub(r'[^a-z0-9]', '', clean_pkg)

        score = 0.0

        if adv_clean and len(adv_clean) > 2 and adv_clean in clean_pkg:
            score += 0.5

        if head_clean and len(head_clean) > 2:
            if head_clean in clean_pkg:
                score += 0.5
            else:
                ratio = difflib.SequenceMatcher(None, head_clean, clean_pkg).ratio()
                score += (ratio * 0.4) 

        if score > highest_score:
            highest_score = score
            best_pkg = pkg

        combo_ratio = difflib.SequenceMatcher(None, head_clean + adv_clean, clean_pkg).ratio()
        if combo_ratio > highest_score:
            highest_score = combo_ratio
            best_pkg = pkg

    return best_pkg if highest_score >= 0.25 else list(package_list)[0]


# =========================
# ADVERTISER EXTRACTION
# =========================

def extract_advertiser_from_page(page):
    """Strictly extract advertiser name using the reliable class"""
    try:
        loc = page.locator('.advertiser-title')
        if loc.count() > 0:
            text = loc.nth(0).inner_text(timeout=1500).strip()
            if text and len(text) > 1:
                return text
    except Exception:
        pass
    return "N/A"


# =========================
# HEADLINE & DESCRIPTION LOGIC
# =========================

def wait_and_extract_headline_description(page, max_wait_seconds=15):
    """Extract headline and description dynamically based on text size/visibility"""
    js = r"""
    () => {
        let texts = [];
        const elements = document.querySelectorAll('body *');
        
        for (let el of elements) {
            if (el.childElementCount > 0) continue;

            let text = (el.innerText || el.textContent || "").replace(/\n/g, ' ').trim();
            if (text.length < 3 || text.includes('{{')) continue;

            const style = window.getComputedStyle(el);
            if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;

            const rect = el.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {
                texts.push({
                    text: text,
                    fontSize: parseFloat(style.fontSize || '0'),
                    top: rect.top
                });
            }
        }

        if (texts.length === 0) return null;

        texts.sort((a, b) => b.fontSize !== a.fontSize ? b.fontSize - a.fontSize : a.top - b.top);

        let uniqueTexts = [];
        let seen = new Set();
        for (let t of texts) {
            if (!seen.has(t.text)) {
                seen.add(t.text);
                uniqueTexts.push(t.text);
            }
        }

        let headText = uniqueTexts.length > 0 ? uniqueTexts[0] : "N/A";
        let descText = uniqueTexts.length > 1 ? uniqueTexts[1] : "N/A";

        return { headline: headText, description: descText };
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


def extract_image_ad_details(page):
    """Extract details from image ad creative"""
    js = r"""
    () => {
        let result = { headline: "N/A", description: "N/A" };
        let images = document.querySelectorAll('img[alt], img[title]');
        for (let img of images) {
            let alt = (img.alt || "").trim();
            let title = (img.title || "").trim();
            let src = (img.src || "").toLowerCase();
            if (src.includes('pixel') || src.includes('beacon') || src.includes('1x1')) continue;
            if (alt.length < 5 && title.length < 5) continue;
            
            if (alt.length > title.length && alt.length > 5) {
                result.headline = alt.substring(0, 100);
                break;
            } else if (title.length > 5) {
                result.headline = title.substring(0, 100);
                break;
            }
        }
        if (result.headline === "N/A") {
            let elements = document.querySelectorAll('[data-creative], [data-ad], .ad-creative, .ad-image, [role="presentation"]');
            for (let el of elements) {
                let text = (el.innerText || el.textContent || "").trim();
                if (text.length > 10) { 
                    result.headline = text.substring(0, 100); 
                    break;
                }
            }
        }
        return result;
    }
    """
    try:
        return page.evaluate(js)
    except Exception:
        return {"headline": "N/A", "description": "N/A"}


# =========================
# MAIN UNIFIED SCRAPER
# =========================

def scrape_single_url(url_row):
    """Unified scraper focused exclusively on IMAGE and TEXT ads (skips video)"""
    row_num, url = url_row

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-dev-shm-usage", "--disable-web-security"]
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            if "region=" not in url:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}region=anywhere"

            print(f"\n🔍 Row {row_num}: Opening transparency URL")
            safe_add_log(row_number=row_num, status="STARTED", log_type="UNIFIED",
                        url=url, message="Starting scraper")

            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)

            # Step 1: Extract advertiser (Strictly targets .advertiser-title)
            advertiser = extract_advertiser_from_page(page)
            print(f"🏷️  Row {row_num}: Advertiser → {advertiser}")

            # Step 2: Check for IMAGE ad
            image_data = extract_image_ad_details(page)
            has_image_headline = image_data.get("headline") != "N/A" and len(image_data.get("headline", "")) > 3

            if has_image_headline:
                print(f"🖼️  Row {row_num}: Detected as IMAGE AD")
                headline = image_data["headline"]
                description = image_data["description"]
                video_id_col = "Image Ad"
                
                all_packages = extract_package_from_page(page)
                package_name = get_best_matching_package(headline, advertiser, all_packages) if all_packages else "NOT FOUND"
                app_link = f"https://play.google.com/store/apps/details?id={package_name}" if package_name != "NOT FOUND" else "N/A"

            else:
                # Step 3: Check for TEXT ad
                print(f"📄 Row {row_num}: Checking for TEXT AD")
                headline, description = wait_and_extract_headline_description(page, max_wait_seconds=15)
                
                if headline != "N/A" and len(headline) >= 3:
                    video_id_col = "Text Ad"
                    all_packages = extract_package_from_page(page)
                    package_name = get_best_matching_package(headline, advertiser, all_packages) if all_packages else "NOT FOUND"
                    app_link = f"https://play.google.com/store/apps/details?id={package_name}" if package_name != "NOT FOUND" else "N/A"
                else:
                    # Non-text and non-image (e.g., skipped video format)
                    print(f"⏭  Row {row_num}: Non-text/Non-image format detected. Skipping extraction.")
                    video_id_col = url  # Leave URL as it is in the Video ID column
                    package_name = "N/A"
                    app_link = "N/A"
                    headline = "N/A"
                    description = "N/A"

            process_time = get_exact_time()

            # Save data
            data = [
                advertiser,
                package_name,
                url,
                app_link,
                process_time,
                video_id_col,
                process_time
            ]
            
            safe_update_combined_row(row_num, data)
            safe_update_headline_desc(row_num, headline, description)
            
            status = "SUCCESS" if video_id_col in ["Text Ad", "Image Ad"] else "SKIPPED_NON_TEXT"
            safe_add_log(row_number=row_num, status=status, log_type="UNIFIED",
                        url=url, video_id=video_id_col, app_link=app_link,
                        message=f"{video_id_col} | Package: {package_name}")
            print(f"✅ Row {row_num}: Saved as {video_id_col}")

        except Exception as e:
            error_time = get_exact_time()
            print(f"❌ Row {row_num} error: {e}")
            try:
                data = ["", "N/A", url, "ERROR", error_time, url, error_time]
                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, "N/A", "N/A")
                safe_add_log(row_number=row_num, status="ERROR", log_type="UNIFIED", url=url, message=str(e))
            except Exception:
                pass
        finally:
            page.close()
            context.close()
            browser.close()


def run_parallel_unified_scraper(max_workers=2):
    """Run unified scraper for all ad types in parallel"""
    urls = sheets.get_urls_with_retry()
    url_rows = [(i + 2, u.strip()) for i, u in enumerate(urls) if u and u.strip()]

    if not url_rows:
        print("No transparency URLs found in sheet.")
        return

    print(f"\n{'='*80}")
    print(f"🚀 UNIFIED AD SCRAPER v3.1 — IMAGE + TEXT")
    print(f"{'='*80}")
    print(f"📊 Starting scraper for {len(url_rows)} rows")
    print(f"⚡ Max workers: {max_workers}")
    print(f"{'='*80}\n")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(scrape_single_url, url_row): url_row for url_row in url_rows}
        for future in as_completed(futures):
            row_num, _ = futures[future]
            try:
                future.result()
            except Exception as e:
                print(f"❌ Worker failed for row {row_num}: {e}")
                try:
                    safe_add_log(row_number=row_num, status="WORKER_ERROR", log_type="UNIFIED",
                                message=str(e))
                except Exception:
                    pass

    print(f"\n{'='*80}")
    print(f"✅ UNIFIED SCRAPER FINISHED")
    print(f"{'='*80}\n")


if __name__ == "__main__":
    run_parallel_unified_scraper(max_workers=MAX_WORKERS)
