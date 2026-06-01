import asyncio
from playwright.async_api import async_playwright
from urllib.parse import urlparse, parse_qs, unquote
from datetime import datetime
import time
import sheets

INSTALL_SELECTORS = ["a.install-button-anchor.svg-anchor", "a.install-button-anchor", 'a[data-asoch-targets-ad-objective-type]', 'a:has-text("Install")', 'a:has-text("Get")', 'a:has-text("Download")']

def get_exact_time():
    return datetime.now().strftime("%I:%M:%S %p")

def clean_googleadservices_link(href):
    if not href: return "N/A"
    href = href.strip()
    if href.startswith("//"): href = "https:" + href
    try:
        parsed = urlparse(href)
        query = parse_qs(parsed.query)
        for key in ["adurl", "url", "q", "u", "ds_dest_url", "destination"]:
            value = query.get(key, [None])[0]
            if value: return unquote(value)
    except Exception:
        pass
    return href

def is_good_app_link(href):
    if not href: return False
    href = href.lower()
    return "googleadservices.com/pagead/aclk" in href or "play.google.com" in href or "apps.apple.com" in href or "itunes.apple.com" in href

def get_current_creative_id(url):
    try:
        parts = url.split("/creative/")
        if len(parts) < 2: return ""
        return parts[1].split("?")[0].split("/")[0].strip()
    except Exception:
        return ""

async def get_visible_install_candidates_from_target(target):
    candidates = []
    for selector in INSTALL_SELECTORS:
        try:
            loc = target.locator(selector)
            count = await loc.count()
            for i in range(count):
                try:
                    el = loc.nth(i)
                    href = await el.get_attribute("href", timeout=1500)
                    data_href = await el.get_attribute("data-href", timeout=1000)
                    final_href = href or data_href

                    if not final_href or not is_good_app_link(final_href): continue

                    box = await el.bounding_box(timeout=1500)
                    if not box or box["width"] < 20 or box["height"] < 10: continue

                    text = ""
                    try:
                        text = (await el.inner_text(timeout=1000)).strip().lower()
                    except Exception:
                        pass

                    score = 0
                    try:
                        class_name = await el.get_attribute("class", timeout=1000) or ""
                        if "install-button-anchor" in class_name: score += 100
                    except Exception:
                        pass

                    if "install" in text: score += 80
                    elif "get" in text or "download" in text: score += 40

                    center_x, center_y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                    if 350 <= center_x <= 850: score += 40
                    if 50 <= center_y <= 700: score += 40
                    if center_y > 700: score -= 100

                    candidates.append({"href": final_href, "score": score, "box": box, "text": text})
                except Exception:
                    continue
        except Exception:
            continue
    return candidates

async def extract_visible_install_link(page):
    all_candidates = []
    try:
        all_candidates.extend(await get_visible_install_candidates_from_target(page))
    except Exception:
        pass

    for frame in page.frames:
        try:
            all_candidates.extend(await get_visible_install_candidates_from_target(frame))
        except Exception:
            continue

    if not all_candidates: return "N/A"
    all_candidates.sort(key=lambda x: x["score"], reverse=True)
    best = all_candidates[0]
    if best["score"] <= 0: return "N/A"
    return clean_googleadservices_link(best["href"])

async def extract_install_link_by_precise_js(page):
    js = """
    () => {
        const anchors = Array.from(document.querySelectorAll('a[href], a[data-href]'));
        const candidates = anchors.map(a => {
            const href = a.href || a.getAttribute('href') || a.getAttribute('data-href') || '';
            const text = (a.innerText || a.textContent || '').trim().toLowerCase();
            const cls = String(a.className || '').toLowerCase();
            const rect = a.getBoundingClientRect();

            const goodLink = href.includes('googleadservices.com/pagead/aclk') || href.includes('play.google.com') || href.includes('apps.apple.com') || href.includes('itunes.apple.com');
            const looksInstall = cls.includes('install-button-anchor') || text.includes('install') || text.includes('get') || text.includes('download');
            const visible = rect.width > 20 && rect.height > 10 && rect.bottom > 0 && rect.right > 0 && rect.top < window.innerHeight && rect.left < window.innerWidth;

            if (!goodLink || !looksInstall || !visible) return null;

            let score = 0;
            if (cls.includes('install-button-anchor')) score += 100;
            if (text.includes('install')) score += 80;
            if (text.includes('get') || text.includes('download')) score += 40;
            
            const cx = rect.left + rect.width / 2, cy = rect.top + rect.height / 2;
            if (cx >= 350 && cx <= 850) score += 40;
            if (cy >= 50 && cy <= 700) score += 40;
            if (cy > 700) score -= 100;

            return { href, score };
        }).filter(Boolean);

        candidates.sort((a, b) => b.score - a.score);
        return candidates.length ? candidates[0].href : null;
    }
    """
    try:
        href = await page.evaluate(js)
        if href and is_good_app_link(href): return clean_googleadservices_link(href)
    except Exception:
        pass

    for frame in page.frames:
        try:
            href = await frame.evaluate(js)
            if href and is_good_app_link(href): return clean_googleadservices_link(href)
        except Exception:
            continue
    return "N/A"

async def wait_and_extract_install_link(page, max_wait_seconds=15):
    start = time.time()
    while time.time() - start < max_wait_seconds:
        app_link = await extract_visible_install_link(page)
        if app_link != "N/A": return app_link

        app_link = await extract_install_link_by_precise_js(page)
        if app_link != "N/A": return app_link

        await asyncio.sleep(1)
    return "N/A"

async def block_media_and_images(route):
    # Aggressively block media. App links rarely need images to render the 'Install' button DOM
    if route.request.resource_type in ["image", "media", "font"]:
        await route.abort()
    else:
        await route.continue_()

async def scrape_single_app_link(context, url_row, sem):
    row_num, url = url_row

    async with sem:
        page = await context.new_page()
        try:
            creative_id = get_current_creative_id(url)
            if "region=" not in url:
                url = f"{url}{'&' if '?' in url else '?'}region=anywhere"

            print(f"🔗 Row {row_num}: opening creative {creative_id}")
            await asyncio.to_thread(sheets.add_log, row_number=row_num, status="STARTED", log_type="APP_LINK", url=url, message="Started checking app link")

            # OPTIMIZATION: Network idle instead of 7-second sleep
            await page.goto(url, wait_until="networkidle", timeout=60000)

            current_url = page.url
            if creative_id and creative_id not in current_url:
                print(f"⚠ Row {row_num}: creative changed, retrying original URL")
                await page.goto(url, wait_until="networkidle", timeout=60000)

            app_link = await wait_and_extract_install_link(page, max_wait_seconds=15)
            app_link_checked_time = get_exact_time()

            if app_link == "N/A":
                print(f"⏭ Row {row_num}: no exact install link found")
                await asyncio.to_thread(sheets.update_app_link, row_num, "N/A", app_link_checked_time)
                await asyncio.to_thread(sheets.add_log, row_number=row_num, status="NOT_FOUND", log_type="APP_LINK", url=url, app_link="N/A", message="No exact visible install link found")
                return

            await asyncio.to_thread(sheets.update_app_link, row_num, app_link, app_link_checked_time)
            await asyncio.to_thread(sheets.add_log, row_number=row_num, status="SUCCESS", log_type="APP_LINK", url=url, app_link=app_link, message="App link saved")
            print(f"✅ Row {row_num}: saved app link")

        except Exception as e:
            app_link_checked_time = get_exact_time()
            print(f"❌ Row {row_num} error: {e}")
            await asyncio.to_thread(sheets.update_app_link, row_num, "ERROR", app_link_checked_time)
            await asyncio.to_thread(sheets.add_log, row_number=row_num, status="ERROR", log_type="APP_LINK", url=url, message=str(e))
        finally:
            await page.close()

async def run_parallel_app_link_scraper(max_workers=1):
    url_rows = sheets.get_video_ad_rows()
    if not url_rows:
        print("No video-ad rows found.")
        return

    print(f"🎬 Found {len(url_rows)} video-ad rows. Extracting exact app links...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage", "--disable-web-security"])
        context = await browser.new_context(viewport={"width": 1366, "height": 768}, user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        
        await context.route("**/*", block_media_and_images)

        sem = asyncio.Semaphore(max_workers)
        tasks = [scrape_single_app_link(context, url_row, sem) for url_row in url_rows]
        
        await asyncio.gather(*tasks)
        await browser.close()

    print("✅ Finished extracting exact app links")

if __name__ == "__main__":
    asyncio.run(run_parallel_app_link_scraper(max_workers=1))