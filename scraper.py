from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs
import time
import sheets
import config

def run_scraper():
    print("Fetching URLs from Google Sheets...")
    urls = sheets.get_urls()
    
    if not urls:
        print("No URLs found to process.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=config.HEADLESS) 
        context = browser.new_context()

        for index, url in enumerate(urls):
            row_num = index + 2  
            print(f"\n--- Processing Row {row_num}: {url} ---")
            
            page = context.new_page()
            captured_video_id = "N/A"

            # ==========================================
            # DEFENSE 2: THE CDN CATCHER & NETWORK INTERCEPTOR
            # ==========================================
            def handle_request(request):
                nonlocal captured_video_id
                req_url = request.url
                
                # Catch 1: Standard YouTube Embeds
                if "youtube.com/embed/" in req_url:
                    try:
                        vid = req_url.split("youtube.com/embed/")[1].split("?")[0]
                        if vid: captured_video_id = vid
                    except: pass
                    
                # Catch 2: YouTube Watch URLs
                elif "youtube.com/watch" in req_url:
                    try:
                        parsed_url = urlparse(req_url)
                        vid = parse_qs(parsed_url.query).get('v', [None])[0]
                        if vid: captured_video_id = vid
                    except: pass
                    
                # Catch 3: Raw Google Video Streams
                elif "googlevideo.com/videoplayback" in req_url:
                    try:
                        parsed_url = urlparse(req_url)
                        query_params = parse_qs(parsed_url.query)
                        vid = query_params.get('docid', query_params.get('id', [None]))[0]
                        if vid and captured_video_id == "N/A":
                            captured_video_id = vid
                    except: pass
                
                # Catch 4: Direct HTML5 Video Files (.mp4 or .m3u8 streams)
                elif req_url.endswith('.mp4') or '.m3u8' in req_url:
                    if captured_video_id == "N/A":
                        # Grab the filename as the ID since it isn't a standard YouTube ID
                        captured_video_id = req_url.split('/')[-1].split('?')[0]

            page.on("request", handle_request)

            try:
                # Navigate to the Ad Center URL
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(2) 
                
                # Scrape the DOM Elements (Text Data)
                try:
                    advertiser = page.locator('div.advertiser-name-selector').first.inner_text(timeout=5000)
                except: advertiser = "N/A"
                    
                try:
                    ad_name = page.locator('h1.ad-title-selector').first.inner_text(timeout=5000)
                except: ad_name = "N/A"
                    
                try:
                    app_link = page.locator('a[href*="play.google.com"], a[href*="apps.apple.com"]').first.get_attribute('href', timeout=5000)
                except: app_link = "N/A"

                # Extract Page Pixels from DOM
                extracted_pixels = []
                try:
                    images = page.locator('img').all()
                    for img in images:
                        src = img.get_attribute('src')
                        if src and src.startswith('http'):
                            extracted_pixels.append(src)
                except Exception as e:
                    print(f"Pixel extraction error: {e}")
                
                pixel_data = ", ".join(extracted_pixels[:3]) if extracted_pixels else "No pixels found"

                # ==========================================
                # DEFENSE 3: THE OVERLAY ANNIHILATOR
                # ==========================================
                # A. Clear standard Google Cookie/Consent banners if they appear
                try:
                    consent_btn = page.locator('button:has-text("Accept all"), button:has-text("Agree")').first
                    if consent_btn.count() > 0:
                        consent_btn.click(timeout=2000)
                        time.sleep(1)
                except: pass

                # B. Inject JS to neutralize invisible tracking shields overlaying the video
                try:
                    page.evaluate('''
                        document.querySelectorAll('*').forEach(el => {
                            const style = window.getComputedStyle(el);
                            if (style.position === 'fixed' || style.position === 'absolute') {
                                // If it is an invisible layer sitting on top of everything, disable its click physics
                                if (style.zIndex > 50 && (style.opacity === '0' || style.backgroundColor === 'rgba(0, 0, 0, 0)')) {
                                    el.style.pointerEvents = 'none';
                                }
                            }
                        });
                    ''')
                except: pass

                # ==========================================
                # DEFENSE 1: THE IFRAME PIERCER & FALLBACKS
                # ==========================================
                click_success = False
                click_strategies = [
                    # Strategy 1: Pierce the iframe and look for the giant YouTube play button
                    page.frame_locator('iframe').locator('.ytp-large-play-button, button[aria-label*="Play"]').first,
                    
                    # Strategy 2: Known CSS Classes
                    page.locator('div.play-button, div.play-button-image').first, 
                    
                    # Strategy 3: Visual SVG matching
                    page.locator('svg').locator('..').first, 
                    
                    # Strategy 4: Brute force center of the ad container
                    page.locator('div[jscontroller], div[role="main"], iframe').first 
                ]

                for target in click_strategies:
                    try:
                        if target.count() > 0 and target.is_visible(timeout=2000):
                            print("▶️ Target acquired, clicking play...")
                            target.click(force=True) 
                            
                            # SMART WAIT: Wait up to 10 seconds for the network to catch the ID
                            wait_time = 0
                            while captured_video_id == "N/A" and wait_time < 10:
                                time.sleep(0.5)
                                wait_time += 0.5
                                
                            if captured_video_id != "N/A":
                                print(f"✅ Intercepted Video ID: {captured_video_id} (took {wait_time}s)")
                                
                            click_success = True
                            break 
                    except Exception:
                        continue 

                if not click_success:
                    print("⏭️ No clickable target found. Likely a static image ad.")

                # Save the data ONLY if it is a video ad
                if captured_video_id != "N/A":
                    data = [advertiser, ad_name, url, app_link, captured_video_id]
                    sheets.update_row(row_num, data)
                    print(f"💾 Saved to Sheet: {data}")

            except Exception as e:
                print(f"❌ Error processing {url}: {e}")
            finally:
                page.close()

        print("Finished processing all URLs.")
        browser.close()

if __name__ == "__main__":
    run_scraper()