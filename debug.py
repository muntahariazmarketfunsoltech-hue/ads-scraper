# debug.py - FIXED for Google Transparency Video Ads
from playwright.sync_api import sync_playwright
import re
import time

# ================== CONFIG ==================
TEST_URL = "https://adstransparency.google.com/advertiser/AR04661836496116908033/creative/CR15503656706658795521"
ROW_NUM = 11
# ===========================================

def extract_video_id_from_url(req_url):
    try:
        url_lower = req_url.lower()
        if any(ext in url_lower for ext in [".mp4", ".webm", ".mov", ".m4v", ".m3u8", "videoplayback"]):
            # Extract ID from Google video URLs
            if "videoplayback" in url_lower or "googlevideo.com" in url_lower:
                import re
                match = re.search(r'id=([^&]+)', req_url)
                if match:
                    return match.group(1)
            return req_url.split("/")[-1].split("?")[0]
    except:
        pass
    return None

def debug_scrape():
    print(f"🔬 DEBUG MODE - Video Ad Fix")
    print(f"🌐 URL: {TEST_URL}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, slow_mo=800)
        context = browser.new_context(viewport={"width": 1400, "height": 900})
        page = context.new_page()

        captured_video = {"id": None}

        def handle_request(req):
            if not captured_video["id"]:
                vid = extract_video_id_from_url(req.url)
                if vid:
                    captured_video["id"] = vid
                    print(f"🎥 VIDEO FOUND: {vid}")

        page.on("request", handle_request)
        page.on("response", handle_request)

        try:
            print("➡️ Loading ad page...")
            page.goto(TEST_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(5000)
            page.screenshot(path="debug_1_loaded.png")

            print("▶️ Trying to play video...")

            # Stronger play button strategies for Google Ads
            play_attempts = [
                'video',
                'div[role="button"]',
                'button',
                '[class*="play"]',
                '[aria-label*="Play"]',
                '[title*="Play"]'
            ]

            for selector in play_attempts:
                try:
                    elements = page.locator(selector).all()
                    print(f"   Checking {selector}: {len(elements)} elements")
                    for el in elements[:8]:
                        if el.is_visible():
                            try:
                                el.scroll_into_view_if_needed(timeout=5000)
                                box = el.bounding_box()
                                if box and box["width"] > 50:
                                    x = box["x"] + box["width"] / 2
                                    y = box["y"] + box["height"] / 2
                                    page.mouse.move(x, y)
                                    page.mouse.click(x, y)
                                    print(f"✅ Clicked play using: {selector}")
                                    page.wait_for_timeout(6000)
                                    break
                            except:
                                continue
                except:
                    continue

            # Extra wait for video to load
            page.wait_for_timeout(8000)
            page.screenshot(path="debug_2_after_play.png")

            # Final aggressive video scan
            if not captured_video["id"]:
                print("🔍 Deep scanning for video URLs...")
                try:
                    resources = page.evaluate("""() => {
                        return performance.getEntriesByType('resource')
                            .map(r => r.name)
                            .filter(url => url.includes('mp4') || url.includes('videoplayback') || 
                                          url.includes('googlevideo') || url.includes('m3u8'));
                    }""")
                    for url in resources:
                        vid = extract_video_id_from_url(url)
                        if vid:
                            captured_video["id"] = vid
                            print(f"🎥 Deep scan found: {vid}")
                            break
                except Exception as e:
                    print(f"Scan error: {e}")

            video_id = captured_video["id"] or "N/A"
            print(f"\n🎯 FINAL VIDEO ID: {video_id}")

            # === Install Button Extraction (Based on your screenshot) ===
            print("\n🔍 Looking for Install Button...")
            app_link = "N/A"
            
            install_selectors = [
                "a.ns-zo4pe-e-30.install-button",
                "a.install-button",
                "a.on-anchor.svg-anchor",
                "a[href*='googleadservices.com/pagead/aclk']",
                "a[class*='install-button']"
            ]

            for sel in install_selectors:
                try:
                    els = page.locator(sel).all()
                    for el in els:
                        href = el.get_attribute("href")
                        if href and "googleadservices.com" in href:
                            app_link = href
                            print(f"✅ INSTALL LINK FOUND:\n{app_link}")
                            break
                except:
                    continue
                if app_link != "N/A":
                    break

            # Try in all frames
            if app_link == "N/A":
                for frame in page.frames:
                    for sel in install_selectors:
                        try:
                            els = frame.locator(sel).all()
                            for el in els:
                                href = el.get_attribute("href")
                                if href and "googleadservices.com" in href:
                                    app_link = href
                                    print(f"✅ FOUND IN FRAME: {app_link}")
                                    break
                        except:
                            continue
                        if app_link != "N/A":
                            break

            print(f"\n📊 FINAL SUMMARY:")
            print(f"   Video ID   : {video_id}")
            print(f"   App Link   : {app_link[:180] + '...' if len(app_link) > 180 else app_link}")

        except Exception as e:
            print(f"❌ ERROR: {e}")
        finally:
            print("\nBrowser is still open. Inspect it manually if needed.")
            input("Press Enter to close... ")
            browser.close()

if __name__ == "__main__":
    debug_scrape()