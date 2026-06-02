import time
from sheets import update_combined_row, add_log
from playwright.sync_api import sync_playwright


def extract_advertiser(page):
    """Extract advertiser name from the top heading."""
    bad_keywords = ['ads transparency centre', 'ad details', 'report this ad']
    selectors = ["h1", "div[role='heading']", "span[role='heading']"]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = loc.count()
            for i in range(count):
                text = loc.nth(i).inner_text(timeout=2000).strip()
                if text and not any(b in text.lower() for b in bad_keywords):
                    return text
        except Exception:
            continue
    return "N/A"


def extract_ad_headline(page):
    """Extract headline of the ad."""
    selectors = ["h2", "div[role='heading']", "span[role='heading']"]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = loc.count()
            for i in range(count):
                text = loc.nth(i).inner_text(timeout=2000).strip()
                if text:
                    return text
        except Exception:
            continue
    return "N/A"


def extract_ad_description(page):
    """Extract description of the ad."""
    selectors = ["p", "span", "div.ad-description"]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            count = loc.count()
            for i in range(count):
                text = loc.nth(i).inner_text(timeout=2000).strip()
                if text and len(text) > 5:
                    return text
        except Exception:
            continue
    return "N/A"


def scrape_single_url(row_data):
    """
    Scrape a single ad page.
    row_data: tuple (row_num, transparency_url)
    """
    row_num, url = row_data
    advertiser = "N/A"
    app_link = ""
    app_link_time = ""
    video_id = ""
    video_time = ""
    ad_headline = "N/A"
    ad_description = "N/A"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(url, timeout=30000)

            # Extract advertiser
            advertiser = extract_advertiser(page)

            # Extract headline & description
            ad_headline = extract_ad_headline(page)
            ad_description = extract_ad_description(page)

            # Example: Extract App link if exists
            try:
                app_el = page.locator("a.install-link").first
                if app_el:
                    app_link = app_el.get_attribute("href")
                    app_link_time = time.strftime("%I:%M:%S %p")
            except Exception:
                pass

            # Example: Extract video id
            try:
                video_el = page.locator("video").first
                if video_el:
                    video_id = video_el.get_attribute("data-video-id")
                    video_time = time.strftime("%I:%M:%S %p")
            except Exception:
                pass

            # Save combined row to sheet
            data = [
                advertiser,        # Column A
                "",                # Column B (name, optional)
                url,               # Column C
                app_link,          # Column D
                app_link_time,     # Column E
                video_id,          # Column F
                video_time,        # Column G
                ad_headline,       # Column M
                ad_description     # Column N
            ]
            update_combined_row(row_num, data)

        except Exception as e:
            add_log(row_number=row_num, status="SCRAPE_ERROR", log_type="SCRAPER", url=url, message=str(e))
        finally:
            browser.close()