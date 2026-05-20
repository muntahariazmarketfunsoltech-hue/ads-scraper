import asyncio
from urllib.parse import urlparse, parse_qs
from playwright.async_api import async_playwright
from sheets import save_to_sheet
from config import HEADLESS, WAIT_TIMEOUT, SHEET_NAME, CREDS_FILE


# --- Step 1: collect all ad URLs from advertiser page ---
async def get_all_ad_urls(advertiser_url, page):
    print(f"\n📋 Collecting ad URLs from: {advertiser_url}")
    await page.goto(advertiser_url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(3000)

    ad_urls = set()
    previous_count = 0
    no_change_count = 0

    while True:
        # collect all creative links visible on page
        links = await page.eval_on_selector_all(
            'a[href*="/creative/"]',
            'elements => elements.map(el => el.href)'
        )

        for link in links:
            ad_urls.add(link)

        print(f"   Found {len(ad_urls)} ads so far...")

        # stop if no new ads found after 3 scrolls
        if len(ad_urls) == previous_count:
            no_change_count += 1
            if no_change_count >= 3:
                print(f"✅ Done collecting — total {len(ad_urls)} ads found")
                break
        else:
            no_change_count = 0

        previous_count = len(ad_urls)

        # scroll down to load more ads
        await page.evaluate("window.scrollBy(0, 1500)")
        await page.wait_for_timeout(2000)

    return list(ad_urls)


# --- Step 2: scrape one individual ad ---
async def scrape_ad(url, page):
    result = {
        "advertiser": "",
        "name": "",
        "ad_url": url,
        "app_link": "",
        "video_id": ""
    }

    async def on_request(request):
        if "googlevideo.com" in request.url and "videoplayback" in request.url:
            parsed = urlparse(request.url)
            params = parse_qs(parsed.query)
            vid = params.get("id", [""])[0]
            if vid:
                result["video_id"] = vid
                print(f"   ✅ Video ID: {vid}")

    page.on("request", on_request)

    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(3000)

    # get advertiser name
    try:
        result["advertiser"] = await page.inner_text(".advertiser-title")
    except:
        print("   ⚠️  Advertiser name not found")

    # get ad name if available
    try:
        result["name"] = await page.inner_text(".creative-title")
    except:
        pass

    # click play button
    try:
        await page.wait_for_selector('.play-button-image', timeout=15000)
        await page.click('.play-button')
        await page.wait_for_timeout(WAIT_TIMEOUT)
        print("   ✅ Play button clicked")
    except:
        print("   ⚠️  Play button not found — skipping video ID")

    # get app link
    try:
        result["app_link"] = await page.get_attribute("a.cta-button", "href")
    except:
        pass

    # remove listener to avoid duplicates on next ad
    page.remove_listener("request", on_request)

    return result


# --- Step 3: main loop ---
async def main():
    # ✏️ paste your advertiser base URL here — no /creative/CR... part
    ADVERTISER_URL = "https://adstransparency.google.com/advertiser/AR04661836496116908033?region=PK"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=HEADLESS)
        page = await browser.new_page()

        # collect all ad URLs from advertiser page
        ad_urls = await get_all_ad_urls(ADVERTISER_URL, page)

        if not ad_urls:
            print("❌ No ad URLs found — check the advertiser URL")
            await browser.close()
            return

        print(f"\n🚀 Starting to scrape {len(ad_urls)} ads...\n")

        # scrape each ad one by one
        for i, url in enumerate(ad_urls, 1):
            print(f"🔍 [{i}/{len(ad_urls)}] Scraping: {url}")
            data = await scrape_ad(url, page)
            print(f"   Data: {data}")
            save_to_sheet(data)
            await asyncio.sleep(2)

        await browser.close()
        print("\n✅ All done! Check your Google Sheet.")


asyncio.run(main())