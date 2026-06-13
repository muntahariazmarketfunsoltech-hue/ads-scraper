# Combined Google Ads Transparency scraper
# Video-ad detection logic is kept from the original scrapper.txt.
# Non-video ads use text/image extraction + package matching from the uploaded non-video files.

from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import difflib
import re

def get_best_matching_package_for_text_ad(headline, description, package_list, min_score=0.70):
    """Matches package names with headline + description using character-level comparison."""
    import difflib
    def clean_text_for_comparison(text):
        if not text or text == "N/A":
            return ""
        return re.sub(r"[^a-z0-9]", "", text.lower())

    ad_text = clean_text_for_comparison(str(headline) + str(description))

    best_pkg = None
    best_score = 0.0

    for pkg in package_list:
        pkg_clean = clean_text_for_comparison(pkg)
        if not pkg_clean:
            continue
        ratio = difflib.SequenceMatcher(None, ad_text, pkg_clean).ratio()
        if ratio > best_score:
            best_score = ratio
            best_pkg = pkg

    if best_score >= min_score:
        return best_pkg, best_score
    return None, best_score

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
    """
    Thread-safe Google Sheet row update.
    Browser scraping runs parallel, but sheet writing is protected.
    """
    with SHEET_LOCK:
        sheets.update_combined_row(row_num, data)


def safe_update_headline_desc(row_num, headline, description):
    """
    Thread-safe Google Sheet row update for Headline and Description in cols M and N.
    """
    with SHEET_LOCK:
        sheets.update_headline_and_description(row_num, headline, description)


def safe_add_log(row_number, status, log_type, url="", video_id="", app_link="", message=""):
    """
    Thread-safe log writing.
    """
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


def extract_package_name(app_link):
    """
    Extracts package name from app store link.
    For Google Play: extracts the 'id' parameter
    For App Store: extracts app ID from URL
    """
    if not app_link or app_link == "N/A":
        return "N/A"
    
    try:
        # Google Play Store format: ...?id=com.example.app
        if "play.google.com" in app_link.lower():
            parsed = urlparse(app_link)
            query = parse_qs(parsed.query)
            package_name = query.get("id", [None])[0]
            if package_name:
                return package_name
        
        # Apple App Store format: ...app/app-name/id123456789
        if "apps.apple.com" in app_link.lower():
            # Extract the ID from the URL path
            match = re.search(r"/id(\d+)", app_link)
            if match:
                return f"id{match.group(1)}"
        
        # If we can't extract, return N/A
        return "N/A"
    
    except Exception:
        return "N/A"


# =========================
# VIDEO ID LOGIC (REVERTED TO YOUR ORIGINAL WORKING LOGIC)
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
    """
    Extracts only clean video IDs or filenames.
    Does NOT return full video links.
    """
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
    """
    Checks actual video elements on page and inside frames.
    """
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
            pass

    return None


def wait_and_extract_headline_description(page, max_wait_seconds=15):
    """
    Waits for and extracts headline/description from video ads.
    This is the VIDEO AD text extraction function.
    """
    try:
        page.wait_for_timeout(1000)  # Small delay for DOM to load

        text_selectors = [
            "span.cWEKkb",
            "div.rgsKxf span",
            "div.AD8qn span",
            "div[data-ad-headline]",
            "h1.title",
            "p.description",
        ]

        headline = "N/A"
        description = "N/A"

        for selector in text_selectors:
            try:
                elements = page.query_selector_all(selector)
                if elements:
                    texts = [el.inner_text() for el in elements]
                    if texts:
                        if headline == "N/A":
                            headline = clean_text(texts[0])
                        if description == "N/A" and len(texts) > 1:
                            description = clean_text(texts[1])
                        if headline != "N/A" and description != "N/A":
                            break
            except Exception:
                continue

        return headline, description

    except Exception:
        return "N/A", "N/A"


def detect_video_id(page, captured_responses):
    """
    Main video detection function combining multiple strategies.
    """
    for response in captured_responses:
        try:
            if is_real_video_response(response):
                video_id = extract_video_id_from_url(response.url)
                if video_id:
                    return video_id
        except Exception:
            pass

    video_id = extract_video_from_dom(page)
    if video_id:
        return video_id

    return "N/A"


def extract_advertiser_from_page(page):
    """
    Extracts advertiser name from page.
    """
    try:
        selectors = [
            "div.V3E2td span",
            "div.QKw3ac span",
            "span[data-advertiser]",
            "h1.kp-header div.kp-header div span",
        ]

        for selector in selectors:
            try:
                element = page.query_selector(selector)
                if element:
                    text = element.inner_text()
                    if text:
                        return clean_text(text)
            except Exception:
                continue

        return "N/A"

    except Exception:
        return "N/A"


# =========================
# TEXT AD EXTRACTION (YOUR WORKING LOGIC - DO NOT CHANGE)
# =========================

def wait_and_extract_text_ad_details(page, max_wait_seconds=15):
    """
    Extracts TEXT AD headline and description.
    Uses the same text element selectors that work for text ads.
    """
    try:
        page.wait_for_timeout(1000)

        text_selectors = [
            "span.cWEKkb",
            "div.rgsKxf span",
            "div.AD8qn span",
            "div[data-ad-headline]",
            "h1.title",
            "p.description",
        ]

        headline = "N/A"
        description = "N/A"

        for selector in text_selectors:
            try:
                elements = page.query_selector_all(selector)
                if elements:
                    texts = [el.inner_text() for el in elements]
                    if texts:
                        if headline == "N/A":
                            headline = clean_text(texts[0])
                        if description == "N/A" and len(texts) > 1:
                            description = clean_text(texts[1])
                        if headline != "N/A" and description != "N/A":
                            break
            except Exception:
                continue

        return {
            "headline": headline,
            "description": description
        }

    except Exception:
        return {
            "headline": "N/A",
            "description": "N/A"
        }


# =========================
# NEW: IMAGE AD EXTRACTION (SAME LOGIC AS TEXT ADS)
# =========================

def wait_and_extract_image_ad_details(page, max_wait_seconds=15):
    """
    Extracts IMAGE AD headline and description.
    Uses the SAME extraction logic as text ads to find headline/description.
    This function specifically targets image ads but uses identical selectors.
    """
    try:
        page.wait_for_timeout(1000)

        # Same text selectors as text ads - they work for both!
        text_selectors = [
            "span.cWEKkb",
            "div.rgsKxf span",
            "div.AD8qn span",
            "div[data-ad-headline]",
            "h1.title",
            "p.description",
        ]

        headline = "N/A"
        description = "N/A"

        for selector in text_selectors:
            try:
                elements = page.query_selector_all(selector)
                if elements:
                    texts = [el.inner_text() for el in elements]
                    if texts:
                        if headline == "N/A":
                            headline = clean_text(texts[0])
                        if description == "N/A" and len(texts) > 1:
                            description = clean_text(texts[1])
                        if headline != "N/A" and description != "N/A":
                            break
            except Exception:
                continue

        return {
            "headline": headline,
            "description": description
        }

    except Exception:
        return {
            "headline": "N/A",
            "description": "N/A"
        }


def is_valid_text_ad(headline, description):
    """
    Checks if extracted text is valid for a text ad.
    """
    return headline != "N/A" and description != "N/A"


def has_visible_image_creative(page):
    """
    Checks if there's a visible image creative on the page.
    """
    try:
        img_selectors = [
            "img.creative-img",
            "div.image-container img",
            "img[data-ad-image]",
            "div.ad-visual img",
        ]

        for selector in img_selectors:
            try:
                elements = page.query_selector_all(selector)
                if elements:
                    for el in elements:
                        try:
                            if el.is_visible():
                                return True
                        except Exception:
                            continue
            except Exception:
                continue

        return False

    except Exception:
        return False


def wait_and_extract_install_link(page, max_wait_seconds=8):
    """
    Waits for and extracts the first visible install/app link.
    """
    try:
        for selector in INSTALL_SELECTORS:
            try:
                if "has-text" in selector:
                    elements = page.query_selector_all(selector)
                    if elements:
                        for el in elements:
                            try:
                                href = el.get_attribute("href")
                                if href and ("play.google.com" in href or "apps.apple.com" in href):
                                    return href
                            except Exception:
                                continue
                else:
                    element = page.query_selector(selector)
                    if element:
                        try:
                            href = element.get_attribute("href")
                            if href and ("play.google.com" in href or "apps.apple.com" in href):
                                return href
                        except Exception:
                            continue
            except Exception:
                continue

        return "N/A"

    except Exception:
        return "N/A"


def extract_package_from_page(page):
    """
    Extracts all package names found on the page.
    """
    try:
        link_elements = page.query_selector_all("a[href*='play.google.com']")
        packages = []

        for el in link_elements:
            try:
                href = el.get_attribute("href")
                pkg = extract_package_name(href)
                if pkg != "N/A":
                    packages.append(pkg)
            except Exception:
                continue

        return list(set(packages))

    except Exception:
        return []


def get_best_matching_package(headline, description, package_list, min_score=0.76):
    """
    Matches package with headline + description. Returns (package_name, score).
    """
    return get_best_matching_package_for_text_ad(headline, description, package_list, min_score)


def scrape_single_url(url_row):
    """
    Main scraper function for a single URL.
    
    Flow:
    1. Check VIDEO first (your original logic)
    2. If no video, check TEXT ad
    3. If no text, check IMAGE ad and extract headline/description for IMAGE too
    """
    row_num, url = url_row
    captured = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)

            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )

            page = context.new_page()

            def log_response(response):
                if is_real_video_response(response):
                    captured.append(response)

            page.on("response", log_response)

            try:
                page.goto(url, wait_until="load", timeout=35000)
            except Exception:
                page.goto(url, wait_until="domcontentloaded", timeout=35000)

            advertiser = extract_advertiser_from_page(page)

            # VIDEO LOGIC: same original flow. No text/image extraction runs before this.
            video_id = detect_video_id(page, captured)
            video_time = get_exact_time()

            # =========================
            # VIDEO AD PATH (UNCHANGED)
            # =========================
            if video_id != "N/A":
                print(f"🎬 Row {row_num}: video ID found first: {video_id}")

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

                data = [
                    advertiser,
                    package_name,
                    url,
                    app_link,
                    app_link_time,
                    video_id,      # Column F: actual video ID for video ads
                    video_time
                ]

                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, headline, description)

                safe_add_log(
                    row_number=row_num,
                    status=status,
                    log_type="VIDEO_AD",
                    url=url,
                    video_id=video_id,
                    app_link=app_link,
                    message=message
                )

                print(f"✅ Row {row_num}: saved VIDEO ad advertiser + package + video ID + text")
                return

            # =========================
            # NON-VIDEO PATH: TEXT + IMAGE ADS
            # =========================
            print(f"📄 Row {row_num}: no video found, checking text/image ad")

            text_data = wait_and_extract_text_ad_details(page, max_wait_seconds=15)
            headline = clean_text(text_data.get("headline"))
            description = clean_text(text_data.get("description"))
            process_time = get_exact_time()
            has_text = is_valid_text_ad(headline, description)

            # First try visible install/app link from the active creative.
            visible_app_link = wait_and_extract_install_link(page, max_wait_seconds=8)
            visible_package = extract_package_name(visible_app_link)

            is_image_like = has_visible_image_creative(page)
            ad_type = "text" if has_text else "image" if (is_image_like or visible_package != "N/A") else "N/A"

            if not has_text and visible_package == "N/A" and not is_image_like:
                data = [
                    advertiser,
                    "N/A",
                    url,
                    "N/A",
                    process_time,
                    "N/A",
                    process_time
                ]

                safe_update_combined_row(row_num, data)
                safe_update_headline_desc(row_num, "N/A", "N/A")

                safe_add_log(
                    row_number=row_num,
                    status="NO_VIDEO_NO_TEXT_IMAGE",
                    log_type="COMBINED",
                    url=url,
                    video_id="N/A",
                    app_link="N/A",
                    message="No video ID and no valid text/image creative found"
                )

                print(f"⏭ Row {row_num}: no video and no valid text/image ad found")
                return

            # =========================
            # NEW: Extract headline/description for IMAGE ADS TOO!
            # =========================
            if is_image_like and not has_text:
                print(f"🖼 Row {row_num}: image ad detected, extracting headline/description using image ad logic")
                image_data = wait_and_extract_image_ad_details(page, max_wait_seconds=15)
                headline = clean_text(image_data.get("headline"))
                description = clean_text(image_data.get("description"))
                
                if headline != "N/A" or description != "N/A":
                    print(f"📝 Row {row_num}: image ad headline -> {headline}, description -> {description}")

            if has_text:
                print(f"🔎 Row {row_num}: text ad headline -> {headline}")
            else:
                print(f"🖼 Row {row_num}: image ad detected (headline: {headline}, description: {description})")

            print(f"📦 Row {row_num}: resolving package from visible install link first")

            if visible_package != "N/A":
                package_name = visible_package
                app_link = visible_app_link
                match_score = 1.0
                status = "SUCCESS"
                message = f"Non-video {ad_type} ad package extracted from visible install link"
                print(f"✅ Row {row_num}: package from visible install link -> {package_name}")
            else:
                package_name = None
                match_score = 0.0

                if has_text:
                    print(f"📦 Row {row_num}: visible install link not found, strict matching with headline + description")
                    all_found_packages = extract_package_from_page(page)
                    package_name, match_score = get_best_matching_package(headline, description, all_found_packages)

                if package_name:
                    app_link = f"https://play.google.com/store/apps/details?id={package_name}"
                    status = "SUCCESS"
                    message = f"Non-video {ad_type} ad package strictly matched with score {match_score}"
                    print(f"✅ Row {row_num}: strict matched package -> {package_name} | score={match_score}")
                else:
                    package_name = "N/A"
                    app_link = "N/A"
                    status = "NON_VIDEO_PACKAGE_NOT_FOUND"
                    message = f"Non-video {ad_type} ad found, but package score below 0.76. Best score={match_score}"
                    print(f"⚠️ Row {row_num}: package score below 0.76, writing N/A | best score={match_score}")

            data = [
                advertiser,
                package_name,
                url,
                app_link,
                process_time,
                ad_type,      # Column F: text/image for non-video ads
                process_time
            ]

            safe_update_combined_row(row_num, data)
            safe_update_headline_desc(row_num, headline, description)

            safe_add_log(
                row_number=row_num,
                status=status,
                log_type="NON_VIDEO_AD",
                url=url,
                video_id=ad_type,
                app_link=app_link,
                message=message
            )

            print(f"✅ Row {row_num}: saved NON-VIDEO {ad_type} ad advertiser + package + headline + description")

        except Exception as e:
            error_time = get_exact_time()
            print(f"❌ Row {row_num} error at {error_time}: {e}")

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

    print(f"🚀 Starting combined VIDEO + TEXT + IMAGE scraper for {len(url_rows)} rows")
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

    print("✅ Finished combined video + text + image scraping")


if __name__ == "__main__":
    run_parallel_combined_scraper(max_workers=MAX_WORKERS)
