import asyncio
from urllib.parse import urlparse, parse_qs
from playwright.async_api import async_playwright
from sheets import save_to_sheet

AD_URL = "https://adstransparency.google.com/advertiser/AR..."  # paste your target URL here

async def scrape_ad(url):
    result = {
        "advertiser": "",
        "name": "",
        "ad_url": url,
        "app_link": "",
        "video_id": ""
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # headed so you can see what's happening
        page = await browser.new_page()

        # Intercept network requests BEFORE navigating
        async def on_request(request):
            if "googlevideo.com" in request.url or "videoplayback" in request.url:
                parsed = urlparse(request.url)
                params = parse_qs(parsed.query)
                # Video ID is often in 'id' or 'docid' param — inspect yours
                vid = params.get("id", params.get("docid", [""]))[0]
                if vid:
                    result["video_id"] = vid
                    print(f"✅ Video ID captured: {vid}")

        page.on("request", on_request)

        await page.goto(url, wait_until="networkidle")

        # --- Grab advertiser name from page ---
        try:
            result["advertiser"] = await page.inner_text(".advertiser-name")  # inspect real class
        except:
            print("⚠️ Advertiser selector not found — inspect the page and update")

        # --- Click the play button ---
        try:
            await page.click('[aria-label="Play"]')  # update selector after inspecting
            await page.wait_for_timeout(4000)  # wait for video network request
        except:
            print("⚠️ Play button not found — check selector")

        # --- Grab app link if present ---
        try:
            result["app_link"] = await page.get_attribute("a.cta-button", "href")
        except:
            pass

        await browser.close()

    return result

async def main():
    data = await scrape_ad(AD_URL)
    print(data)
    save_to_sheet(data)

asyncio.run(main())