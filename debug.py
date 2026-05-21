from playwright.sync_api import sync_playwright

# 🛑 PASTE ONE FAILING URL HERE 🛑
TEST_URL = "https://adstransparency.google.com/advertiser/AR04661836496116908033/creative/CR15063966404757684225"

def run_debug():
    with sync_playwright() as p:
        # We are forcing Playwright to use your actual local Chrome browser 
        # to see if Google is just blocking "Chromium" bots.
        try:
            browser = p.chromium.launch(headless=False, channel="chrome")
        except:
            print("Chrome channel failed, falling back to standard Chromium...")
            browser = p.chromium.launch(headless=False)
            
        context = browser.new_context()
        page = context.new_page()

        print("\n📡 Listening to ALL media network traffic...")
        # Catch 3: ALL Raw Video Streams (Broadened)
        elif "videoplayback" in req_url:
        try:
                        parsed_url = urlparse(req_url)
                        query_params = parse_qs(parsed_url.query)
                        # Check for 'id' first (as seen in your screenshot), then fallback to 'docid'
                        vid = query_params.get('id', query_params.get('docid', [None]))[0]
                        if vid and captured_video_id == "N/A":
                            captured_video_id = vid
        except: pass
        # We are going to print EVERYTHING that even slightly resembles a video
        def log_request(request):
            url = request.url
            if any(keyword in url for keyword in ['googlevideo', 'youtube', '.mp4', '.m3u8', 'play', 'video']):
                print(f"\n🚨 CAUGHT SUSPICIOUS NETWORK REQUEST:\n{url}")

        page.on("request", log_request)

        print(f"\n🌐 Navigating to target...")
        page.goto(TEST_URL, wait_until="domcontentloaded")

        print("\n⏸️ SCRIPT PAUSED.")
        print("👉 Look at the Chromium browser window that just popped up.")
        print("👉 Do not close the Playwright Inspector window.")
        print("👉 MANUAL TEST: Click the play button on the ad YOURSELF with your mouse.")
        
        # This completely freezes the script and opens Playwright's debugger tool
        page.pause() 

        browser.close()

if __name__ == "__main__":
    run_debug()