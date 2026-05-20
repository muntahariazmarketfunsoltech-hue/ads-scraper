from playwright.sync_api import sync_playwright
from urllib.parse import urlparse, parse_qs
import time
import sheets
import config # <--- Add this line

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
            row_num = index + 2  # +2 because index is 0-based and row 1 is headers
            print(f"--- Processing Row {row_num} ---")
            
            page = context.new_page()
            captured_video_id = "N/A"

            # 1. Setup Network Interception
            def handle_request(request):
                nonlocal captured_video_id
                if "googlevideo.com/videoplayback" in request.url:
                    try:
                        # Extract the 'id' parameter from the Google Video URL
                        parsed_url = urlparse(request.url)
                        video_id = parse_qs(parsed_url.query).get('id', [None])[0]
                        if video_id:
                            captured_video_id = video_id
                            print(f"✅ Intercepted Video ID: {captured_video_id}")
                    except Exception as e:
                        pass

            # Attach the interceptor to the page
            page.on("request", handle_request)

            try:
                # 2. Navigate to the Ad Center URL
                # 2. Navigate to the Ad Center URL
                # "domcontentloaded" finishes as soon as the HTML arrives, ignoring background trackers
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                
                # Give the heavy JavaScript framework a brief moment to render the UI components
                time.sleep(2)
                
                # 3. Scrape the DOM Elements
                # NOTE: These selectors need to be adjusted to match Google's current layout
                try:
                    advertiser = page.locator('div.advertiser-name-selector').first.inner_text(timeout=5000)
                except:
                    advertiser = "N/A"
                    
                try:
                    ad_name = page.locator('h1.ad-title-selector').first.inner_text(timeout=5000)
                except:
                    ad_name = "N/A"
                    
                try:
                    app_link = page.locator('a[href*="play.google.com"], a[href*="apps.apple.com"]').first.get_attribute('href', timeout=5000)
                except:
                    app_link = "N/A"

                # 4. Find the Play Button and trigger the network request
               # 4. Find the Play Button and trigger the network request
                # Updated to look for the specific div classes from your screenshot
               # 4. Robust Play Button Trigger (Fallback Chain)
                click_success = False
                
                # List of locators, ordered from most specific to the broadest brute-force option
                click_strategies = [
                    # Strategy 1: The exact known classes
                    page.locator('div.play-button, div.play-button-image').first, 
                    
                    # Strategy 2: Look for any container that is directly wrapping an SVG icon (often the play triangle)
                    page.locator('svg').locator('..').first, 
                    
                    # Strategy 3: Brute force - find the main visual area of the page and click the center
                    page.locator('div[jscontroller], div[role="main"], iframe').first 
                ]

                for target in click_strategies:
                    try:
                        # Check if this specific target exists and is visible
                        if target.count() > 0 and target.is_visible(timeout=2000):
                            print("Target acquired, attempting click...")
                            
                            # force=True tells Playwright to click even if Google placed an invisible overlay on top to block bots
                            target.click(force=True) 
                            
                            time.sleep(config.WAIT_TIMEOUT)
                            click_success = True
                            break # Success! Exit the fallback loop and proceed to save the data
                    except Exception:
                        # If this strategy fails, silently catch the error and move to the next target in the list
                        continue 

                if not click_success:
                    print("No clickable target found. It is likely a static image ad.")

                # 5. Save the data
              # 5. Save the data ONLY if it is a video ad
                if captured_video_id != "N/A":
                    data = [advertiser, ad_name, url, app_link, captured_video_id]
                    sheets.update_row(row_num, data)
                    print(f"💾 Saved Video Ad to Sheet: {data}")
                else:
                    print(f"⏭️ Skipped (Not a video ad): {url}")

            except Exception as e:
                print(f"❌ Error processing {url}: {e}")
            finally:
                page.close()

        print("Finished processing all URLs.")
        browser.close()

if __name__ == "__main__":
    run_scraper()