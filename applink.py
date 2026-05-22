import asyncio
import json
import os
import re
from playwright.async_api import async_playwright
import sheets

MAX_CONCURRENT = 2
SHEET_BATCH_SIZE = 9
COOKIES_FILE = "cookies.json"

def load_cookies():
    if not os.path.exists(COOKIES_FILE):
        print(f"⚠️ No {COOKIES_FILE} found — running without cookies")
        return []
    with open(COOKIES_FILE, "r", encoding="utf-8") as f:
        cookies = json.load(f)
    return cookies

async def scrape_single_url(sem, browser, cookies, row_num, url):
    async with sem:
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Asia/Karachi",
        )
        if cookies:
            await context.add_cookies(cookies)

        page = await context.new_page()
        found_link = "N/A"

        # ---------------------------------------------------------
        # NEW STRATEGY: Network Interception 
        # Listen to all background traffic and catch the App Link
        # ---------------------------------------------------------
        async def handle_response(response):
            nonlocal found_link
            # If we already found the link, ignore further traffic
            if found_link != "N/A":
                return
                
            # Only inspect text/JSON traffic (ignore images, videos, etc.)
            if response.request.resource_type in ["fetch", "xhr", "script", "document"]:
                try:
                    text = await response.text()
                    # Google often escapes slashes in JSON like "https:\/\/play..."
                    clean_text = text.replace('\\/', '/')
                    
                    # Search the raw server data for a Google Play Store link
                    match = re.search(r'(https://play\.google\.com/store/apps/details\?id=[a-zA-Z0-9\._-]+)', clean_text)
                    if match:
                        found_link = match.group(1)
                except Exception:
                    pass # Ignore requests that fail or can't be read

        # Attach our wiretap to the page
        page.on("response", handle_response)
        
        try:
            print(f"\n▶ Row {row_num}: {url}")
            
            # Wait for "networkidle" so all background APIs finish loading
            await page.goto(url, wait_until="networkidle", timeout=45000)
            
            # Give it a tiny bit of extra time to process the last requests
            await page.wait_for_timeout(3000) 
            
            if found_link != "N/A":
                print(f"✅ Row {row_num} — {found_link}")
            else:
                print(f"⏭ Row {row_num} — FAILED: Scanned all network traffic, no Play Store link found.")
                
            return row_num, found_link
            
        except Exception as e:
            print(f"❌ Row {row_num} error: {e}")
            return row_num, "N/A"
        finally:
            # Clean up listeners and browser context to prevent memory leaks
            page.remove_listener("response", handle_response)
            await page.close()
            await context.close()

def write_link_to_sheet(row_num, link):
    sheet = sheets.get_sheet()
    # Writes the link directly into Column D of the exact matching row
    sheet.update(f"D{row_num}", [[link]])

async def write_batch_to_sheets(batch):
    for row_num, link in batch:
        if not link or link in ("N/A", "BLOCKED"):
            continue
        for attempt in range(1, 4):
            try:
                await asyncio.to_thread(write_link_to_sheet, row_num, link)
                print(f"📝 Sheets: row {row_num} col D written")
                break
            except Exception as e:
                print(f"⚠️ Sheets row {row_num} attempt {attempt}/3: {e}")
                if attempt < 3:
                    await asyncio.sleep(3 * attempt)
                else:
                    print(f"❌ Sheets gave up on row {row_num}")

async def run_link_scraper():
    # Fetch the directly mapped (row_number, url) tuples to prevent drifting
    url_rows = sheets.get_urls_with_rows()
    
    if not url_rows:
        print("No URLs to process.")
        return

    cookies = load_cookies()
    print(f"🍪 Loaded {len(cookies)} cookies" if cookies else "⚠️ No cookies loaded")
    print(f"📋 Found {len(url_rows)} URLs — {MAX_CONCURRENT} browsers in parallel\n")

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    results_lock = asyncio.Lock()
    pending_results = []

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=False, channel="chrome", args=["--start-minimized"])
        except Exception:
            browser = await p.chromium.launch(headless=False)

        async def task_wrapper(row_num, url):
            result = await scrape_single_url(sem, browser, cookies, row_num, url)
            async with results_lock:
                pending_results.append(result)
                if len(pending_results) >= SHEET_BATCH_SIZE:
                    batch = pending_results[:SHEET_BATCH_SIZE]
                    del pending_results[:SHEET_BATCH_SIZE]
                    print(f"\n📤 Writing batch of {len(batch)} rows to Sheets...")
                    await write_batch_to_sheets(batch)
                    print("✅ Batch written\n")

        await asyncio.gather(*[task_wrapper(row_num, url) for row_num, url in url_rows])
        
        # Write any remaining URLs that didn't fill up the final batch
        if pending_results:
            print(f"\n📤 Writing final {len(pending_results)} rows to Sheets...")
            await write_batch_to_sheets(pending_results)
            print("✅ Final batch written")

        await browser.close()
    print("\n✅ All done.")

if __name__ == "__main__":
    asyncio.run(run_link_scraper())