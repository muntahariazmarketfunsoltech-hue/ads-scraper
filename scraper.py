from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import difflib
import re
import time
import threading
import json
import sheets

MAX_WORKERS = 2
SHEET_LOCK = threading.Lock()

# =========================
# SHEET WRITING HELPERS
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


# =========================
# AD RESULT FORMATTER
# =========================

class AdResult:
    """Unified ad result with all extracted data"""
    def __init__(self):
        self.advertiser = "N/A"
        self.package_name = "N/A"
        self.app_link = "N/A"
        self.headline = "N/A"
        self.description = "N/A"
        self.ad_type = "N/A"  # TEXT or IMAGE
        self.source_url = "N/A"
        self.extraction_time = get_exact_time()
    
    def display_summary(self, row_num):
        """Print a formatted summary of the ad data"""
        print(f"\n{'='*80}")
        print(f"ROW {row_num} — AD EXTRACTION SUMMARY")
        print(f"{'='*80}")
        print(f"  Type        : {self.ad_type}")
        print(f"  Advertiser  : {self.advertiser}")
        print(f"  Headline    : {self.headline}")
        print(f"  Description : {self.description}")
        print(f"  Package     : {self.package_name}")
        print(f"  App Link    : {self.app_link}")
        print(f"  Time        : {self.extraction_time}")
        print(f"{'='*80}\n")
    
    def to_csv_row(self):
        """Format as CSV row"""
        return [
            self.advertiser,
            self.headline,
            self.description,
            self.package_name,
            self.app_link,
            self.ad_type,
            self.source_url,
            self.extraction_time
        ]
    
    def to_dict(self):
        """Format as dictionary for JSON output"""
        return {
            "advertiser": self.advertiser,
            "headline": self.headline,
            "description": self.description,
            "package_name": self.package_name,
            "app_link": self.app_link,
            "ad_type": self.ad_type,
            "source_url": self.source_url,
            "extraction_time": self.extraction_time
        }


# =========================
# STRING SIMILARITY MATCHER
# =========================

def clean_text_for_comparison(text):
    """Strips spaces, punctuation, and makes text lowercase for a pure letter-to-letter comparison."""
    if not text or text == "N/A": return ""
    return re.sub(r'[^a-z0-9]', '', str(text).lower())

def get_best_matching_package(headline, advertiser, package_list):
    """
    Compares the visible headline/advertiser against all found package names
    and returns the one with the highest similarity score (90% threshold).
    Returns None if no match meets the 90% threshold.
    """
    if not package_list: 
        return None
    
    # If we only found one package on the whole page, just use it
    if len(package_list) == 1: 
        return list(package_list)[0]

    best_pkg = None
    highest_ratio = 0.0
    THRESHOLD = 0.90

    # Combine the visible text we know is on the screen
    visible_target = clean_text_for_comparison(f"{headline}{advertiser}")

    for pkg in package_list:
        # Clean up the package name (remove com., net., android., etc.)
        clean_pkg = re.sub(r'^(com\.|net\.|org\.|android\.)', '', pkg.lower())
        clean_pkg = re.sub(r'[^a-z0-9]', '', clean_pkg)

        # Calculate how similar the letters are (0.0 to 1.0)
        ratio = difflib.SequenceMatcher(None, visible_target, clean_pkg).ratio()
        
        # If it's the best match so far and meets threshold, save it
        if ratio > highest_ratio:
            highest_ratio = ratio
            best_pkg = pkg if ratio >= THRESHOLD else None

    return best_pkg if highest_ratio >= THRESHOLD else None


# =========================
# PACKAGE NAME EXTRACTOR (ENHANCED)
# =========================

def decode_all(text, passes=3):
    """Decode every encoding variant so no package name is missed."""
    for _ in range(passes):
        prev = text
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
    if _SKIP_EXT.search(pkg):            
        return False
    if _SKIP_PFX.match(pkg):             
        return False
    for p in parts:
        if not p or not re.match(r'^[A-Za-z_][A-Za-z0-9_]*$', p):
            return False
    return True

def extract_packages_from_text(raw_text):
    """Returns a SET of all unique, valid package names found in the text."""
    text = decode_all(raw_text)
    candidates = set()   

    patterns = [
        r"""['"](?:appId|bundleId|appPackage|app_package|applicationId|packageName)['"]\s*:\s*['"]([A-Za-z][\w.]+)['"]""",
        r"""play\.google\.com/store/apps/details[^\s'"<>]*[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){1,})""",
        r"""market://[^\s'"]*[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){1,})""",
        r"""(?:destination_url|final_url|click_url|destUrl|clickUrl|landingUrl|adurl|redirect)['"\s]*:?['"\s]*['"]([^'"]{10,}[?&]id=[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){1,}[^'"]*)['"]""",
        r"""[?&]id=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){1,})""",
        r"""[?&]package=([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*){1,})"""
    ]

    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            pkg = m.group(1).rstrip('.,;\'"\\ ')
            # Extract just the package ID from URLs if needed
            if '?' in pkg or '&' in pkg:
                id_match = re.search(r'[?&]id=([A-Za-z][A-Za-z0-9_.]*)', pkg)
                if id_match:
                    pkg = id_match.group(1)
            if _is_valid_pkg(pkg):
                candidates.add(pkg)

    return candidates

def extract_package_from_page(page):
    """
    Scans strictly the rendered DOM and visible links. 
    """
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


# =========================
# ADVERTISER LOGIC 
# =========================

def extract_advertiser_from_page(page):
    """Extract advertiser name with multiple fallback strategies"""
    # Strategy 1: Look for known selectors
    try:
        loc = page.locator('.advertiser-title, [data-test-id="advertiser-name"]').first
        loc.wait_for(timeout=4000)
        text = loc.inner_text().strip()
        if text and len(text) > 1 and "Sign in" not in text:
            return text
    except Exception:
        pass

    # Strategy 2: Find largest/most prominent text
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


# =========================
# TEXT AD EXTRACTION
# =========================

def wait_and_extract_text_ad_details(page, max_wait_seconds=15):
    """Extract headline and description from text ads with visual validation"""
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
        
        // 1. EXTRACT HEADLINE (largest visible text)
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
            
            // 2. EXTRACT DESCRIPTION (longest visible text after headline)
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
# IMAGE AD EXTRACTION
# =========================

def extract_image_ad_details(page):
    """Extract headline and description from image ad creative"""
    js = r"""
    () => {
        let result = { headline: "N/A", description: "N/A" };
        
        // Look for alt text and title attributes in images
        let images = document.querySelectorAll('img[alt], img[title]');
        for (let img of images) {
            let alt = (img.alt || "").trim();
            let title = (img.title || "").trim();
            let src = (img.src || "").toLowerCase();
            
            // Skip tracking pixels and decorative images
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
        
        // Look for visible text near image/creative area
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
        data = page.evaluate(js)
        return data
    except Exception:
        return {"headline": "N/A", "description": "N/A"}


# =========================
# MAIN TEXT AD SCRAPER
# =========================

def scrape_single_text_ad(url_row):
    """Scrape text ad with full data extraction"""
    row_num, url = url_row
    result = AdResult()
    result.source_url = url
    result.ad_type = "TEXT"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-dev-shm-usage", "--disable-web-security"]
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            if "region=" not in url:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}region=anywhere"

            print(f"📄 Row {row_num}: Opening TEXT AD URL")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)

            print(f"🔎 Row {row_num}: Extracting advertiser...")
            result.advertiser = extract_advertiser_from_page(page)

            print(f"📝 Row {row_num}: Extracting headline & description...")
            text_data = wait_and_extract_text_ad_details(page, max_wait_seconds=15)
            result.headline = text_data["headline"]
            result.description = text_data["description"]

            if result.headline == "N/A" or len(result.headline) < 3:
                print(f"⏭  Row {row_num}: No valid text ad headline found. Skipping.")
                return  

            print(f"📦 Row {row_num}: Finding all package names...")
            all_found_packages = extract_package_from_page(page)
            
            print(f"🔍 Row {row_num}: Matching package to headline...")
            package_name = get_best_matching_package(result.headline, result.advertiser, all_found_packages)

            if package_name:
                result.package_name = package_name
                result.app_link = f"https://play.google.com/store/apps/details?id={package_name}"
                print(f"✅ Row {row_num}: Best Matched Package → {package_name}")
            else:
                print(f"⚠️  Row {row_num}: No package matched 90% threshold")

            # Display summary
            result.display_summary(row_num)

            # Update sheet
            data = [
                result.advertiser,     
                result.package_name,  
                url,            
                result.app_link,       
                result.extraction_time,   
                result.ad_type,      
                result.extraction_time,   
            ]
            safe_update_combined_row(row_num, data)
            safe_update_headline_desc(row_num, result.headline, result.description)
            safe_add_log(
                row_number=row_num, status="SUCCESS", log_type=result.ad_type,
                url=url, video_id=result.ad_type,
                app_link=result.app_link,
                message=f"Package: {result.package_name} | Headline: {result.headline[:50]}"
            )
            print(f"✅ Row {row_num}: Data saved successfully")

        except Exception as e:
            error_time = get_exact_time()
            print(f"❌ Row {row_num} error: {e}")
            try:
                data = [result.advertiser, "ERROR", url, "ERROR", error_time, "ERROR", error_time]
                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, "N/A", "N/A")
                safe_add_log(row_number=row_num, status="ERROR", log_type="TEXT_AD", url=url, message=str(e))
            except Exception:
                pass
        finally:
            page.close()
            context.close()
            browser.close()


# =========================
# MAIN IMAGE AD SCRAPER
# =========================

def scrape_single_image_ad(url_row):
    """Scrape image ad with full data extraction"""
    row_num, url = url_row
    result = AdResult()
    result.source_url = url
    result.ad_type = "IMAGE"

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-dev-shm-usage", "--disable-web-security"]
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        try:
            if "region=" not in url:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}region=anywhere"

            print(f"🖼️  Row {row_num}: Opening IMAGE AD URL")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

            # Wait for images to load
            page.wait_for_timeout(2000)

            print(f"🔎 Row {row_num}: Extracting advertiser...")
            result.advertiser = extract_advertiser_from_page(page)

            print(f"🎨 Row {row_num}: Extracting image ad creative details...")
            image_data = extract_image_ad_details(page)
            result.headline = image_data["headline"]
            result.description = image_data["description"]

            print(f"📦 Row {row_num}: Finding all package names...")
            all_found_packages = extract_package_from_page(page)
            
            if all_found_packages:
                print(f"🔍 Row {row_num}: Matching package...")
                package_name = get_best_matching_package(result.headline, result.advertiser, all_found_packages)
                
                if package_name:
                    result.package_name = package_name
                    result.app_link = f"https://play.google.com/store/apps/details?id={package_name}"
                    print(f"✅ Row {row_num}: Package found → {package_name}")
                else:
                    # If no match, use first package found
                    result.package_name = list(all_found_packages)[0]
                    result.app_link = f"https://play.google.com/store/apps/details?id={result.package_name}"
                    print(f"✅ Row {row_num}: Using first found package → {result.package_name}")
            else:
                print(f"⚠️  Row {row_num}: No packages found in this image ad")

            # Display summary
            result.display_summary(row_num)

            # Update sheet
            data = [
                result.advertiser,     
                result.package_name,  
                url,            
                result.app_link,       
                result.extraction_time,   
                result.ad_type,      
                result.extraction_time,   
            ]
            safe_update_combined_row(row_num, data)
            safe_update_headline_desc(row_num, result.headline, result.description)
            safe_add_log(
                row_number=row_num, status="SUCCESS", log_type=result.ad_type,
                url=url, video_id=result.ad_type,
                app_link=result.app_link,
                message=f"Package: {result.package_name} | Found {len(all_found_packages)} package(s)"
            )
            print(f"✅ Row {row_num}: Image ad data saved successfully")

        except Exception as e:
            error_time = get_exact_time()
            print(f"❌ Row {row_num} error: {e}")
            try:
                data = [result.advertiser, "ERROR", url, "ERROR", error_time, "ERROR", error_time]
                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, "N/A", "N/A")
                safe_add_log(row_number=row_num, status="ERROR", log_type="IMAGE_AD", url=url, message=str(e))
            except Exception:
                pass
        finally:
            page.close()
            context.close()
            browser.close()


# =========================
# PARALLEL RUNNER
# =========================

def run_parallel_text_scraper(max_workers=2):
    """Run text ad scraper for multiple URLs in parallel"""
    urls = sheets.get_urls_with_retry()
    url_rows = [(i + 2, u.strip()) for i, u in enumerate(urls) if u and u.strip()]

    if not url_rows:
        print("No transparency URLs found in sheet.")
        return

    print(f"🚀 Starting TEXT AD scraper for {len(url_rows)} rows (Max workers: {max_workers})")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(scrape_single_text_ad, url_row): url_row for url_row in url_rows}
        for future in as_completed(futures):
            row_num, _ = futures[future]
            try:
                future.result()
            except Exception as e:
                print(f"❌ Worker failed for row {row_num}: {e}")

    print("✅ Finished Text Ad scraping")


def run_parallel_image_scraper(max_workers=2):
    """Run image ad scraper for multiple URLs in parallel"""
    urls = sheets.get_urls_with_retry()
    url_rows = [(i + 2, u.strip()) for i, u in enumerate(urls) if u and u.strip()]

    if not url_rows:
        print("No transparency URLs found in sheet.")
        return

    print(f"🎨 Starting IMAGE AD scraper for {len(url_rows)} rows (Max workers: {max_workers})")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(scrape_single_image_ad, url_row): url_row for url_row in url_rows}
        for future in as_completed(futures):
            row_num, _ = futures[future]
            try:
                future.result()
            except Exception as e:
                print(f"❌ Worker failed for row {row_num}: {e}")

    print("✅ Finished Image Ad scraping")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == "image":
        run_parallel_image_scraper(max_workers=MAX_WORKERS)
    else:
        run_parallel_text_scraper(max_workers=MAX_WORKERS)
