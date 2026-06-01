from playwright.sync_api import sync_playwright
from datetime import datetime
import time
import sheets

# Import functions from your main scraper file.
# If your main scraper file has another name, change "scraper" below.
from scraper import (
    is_real_video_response,
    extract_video_id_from_url,
    extract_video_from_dom,
    click_possible_video_targets,
    wait_for_video_id,
    scan_browser_performance_for_video,
    extract_advertiser_and_title,
)


TEST_URL = "PASTE_YOUR_SINGLE_TRANSPARENCY_LINK_HERE"

debug_logs = []


def get_exact_time():
    return datetime.now().strftime("%I:%M:%S %p")


def format_duration(start_time):
    seconds = round(time.time() - start_time, 2)

    if seconds < 60:
        return f"{seconds}s"

    minutes = int(seconds // 60)
    remaining_seconds = round(seconds % 60, 2)

    return f"{minutes}m {remaining_seconds}s"


def log_step(row_num, status, log_type, url, start_time, video_id="", app_link="", message=""):
    time_taken = format_duration(start_time)
    exact_time = get_exact_time()

    print(f"[{exact_time}] [{time_taken}] Row {row_num} | {status} | {message}")

    debug_logs.append([
        exact_time,
        row_num,
        status,
        log_type,
        url,
        video_id,
        app_link,
        time_taken,
        message
    ])


def flush_logs():
    try:
        sheets.add_step_logs_bulk(debug_logs)
        print(f"✅ Wrote {len(debug_logs)} debug logs to StepLogs")
    except Exception as e:
        print(f"⚠ Could not write StepLogs in bulk: {e}")


def debug_single_link(url):
    row_num = "DEBUG"
    row_start_time = time.time()

    if not url or url == "PASTE_YOUR_SINGLE_TRANSPARENCY_LINK_HERE":
        print("❌ Please paste your transparency link in TEST_URL first.")
        return

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )

        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )

        page = context.new_page()
        captured = {"video_id": "N/A"}

        def handle_response(response):
            try:
                if not is_real_video_response(response):
                    return

                video_id = extract_video_id_from_url(response.url)

                if video_id and captured["video_id"] == "N/A":
                    captured["video_id"] = video_id

                    log_step(
                        row_num=row_num,
                        status="NETWORK_VIDEO_CAPTURED",
                        log_type="VIDEO",
                        url=url,
                        start_time=row_start_time,
                        video_id=video_id,
                        message="Video-like network response was captured."
                    )

            except Exception:
                pass

        page.on("response", handle_response)

        try:
            if "region=" not in url:
                separator = "&" if "?" in url else "?"
                url = f"{url}{separator}region=anywhere"

            log_step(
                row_num=row_num,
                status="STARTED",
                log_type="DEBUG",
                url=url,
                start_time=row_start_time,
                message="Started debugging this single transparency link."
            )

            log_step(
                row_num=row_num,
                status="OPENING_PAGE",
                log_type="PAGE",
                url=url,
                start_time=row_start_time,
                message="Opening transparency page in visible browser."
            )

            page.goto(url, wait_until="domcontentloaded", timeout=60000)

            log_step(
                row_num=row_num,
                status="DOM_LOADED",
                log_type="PAGE",
                url=url,
                start_time=row_start_time,
                message="Page DOM loaded."
            )

            page.wait_for_timeout(4000)

            log_step(
                row_num=row_num,
                status="WAITED_FOR_PREVIEW",
                log_type="PAGE",
                url=url,
                start_time=row_start_time,
                message="Waited 4 seconds for ad preview and iframe to load."
            )

            # TWO-ATTEMPT VIDEO CHECK
            log_step(
                row_num=row_num,
                status="CHECKING_DOM_VIDEO",
                log_type="VIDEO",
                url=url,
                start_time=row_start_time,
                message="Checking page and iframes for video elements."
            )

            video_id = extract_video_from_dom(page)

            if video_id != "N/A":
                log_step(
                    row_num=row_num,
                    status="DOM_VIDEO_FOUND",
                    log_type="VIDEO",
                    url=url,
                    start_time=row_start_time,
                    video_id=video_id,
                    message="Video ID was found from DOM video element."
                )

            # Attempt 1
            if video_id == "N/A":
                log_step(
                    row_num=row_num,
                    status="ATTEMPT_1_CLICKING_VIDEO_AREA",
                    log_type="VIDEO",
                    url=url,
                    start_time=row_start_time,
                    message="Attempt 1: clicking possible video preview area."
                )

                clicked = click_possible_video_targets(page)

                log_step(
                    row_num=row_num,
                    status="ATTEMPT_1_CLICK_DONE",
                    log_type="VIDEO",
                    url=url,
                    start_time=row_start_time,
                    message=f"Attempt 1 completed. Clicked target: {clicked}"
                )

                log_step(
                    row_num=row_num,
                    status="ATTEMPT_1_WAITING",
                    log_type="VIDEO",
                    url=url,
                    start_time=row_start_time,
                    message="Attempt 1: waiting 8 seconds to catch video request or DOM video."
                )

                video_id = wait_for_video_id(page, captured, max_seconds=8)

                if video_id != "N/A":
                    log_step(
                        row_num=row_num,
                        status="VIDEO_FOUND_ATTEMPT_1",
                        log_type="VIDEO",
                        url=url,
                        start_time=row_start_time,
                        video_id=video_id,
                        message="Video ID was found during attempt 1."
                    )

            # Performance check after attempt 1 only
            if video_id == "N/A":
                log_step(
                    row_num=row_num,
                    status="CHECKING_PERFORMANCE_AFTER_ATTEMPT_1",
                    log_type="VIDEO",
                    url=url,
                    start_time=row_start_time,
                    message="Attempt 1 did not find video. Checking browser performance resource URLs once."
                )

                video_id = scan_browser_performance_for_video(page)

                if video_id != "N/A":
                    log_step(
                        row_num=row_num,
                        status="VIDEO_FOUND_IN_PERFORMANCE",
                        log_type="VIDEO",
                        url=url,
                        start_time=row_start_time,
                        video_id=video_id,
                        message="Video ID was found from browser performance resources after attempt 1."
                    )

            # Attempt 2 - only click and check video, no extra steps
            if video_id == "N/A":
                log_step(
                    row_num=row_num,
                    status="ATTEMPT_2_QUICK_VIDEO_CHECK",
                    log_type="VIDEO",
                    url=url,
                    start_time=row_start_time,
                    message="Attempt 2: only clicking video area again and checking for video ID."
                )

                clicked = click_possible_video_targets(page)

                log_step(
                    row_num=row_num,
                    status="ATTEMPT_2_CLICK_DONE",
                    log_type="VIDEO",
                    url=url,
                    start_time=row_start_time,
                    message=f"Attempt 2 click completed. Clicked target: {clicked}"
                )

                video_id = wait_for_video_id(page, captured, max_seconds=6)

                if video_id != "N/A":
                    log_step(
                        row_num=row_num,
                        status="VIDEO_FOUND_ATTEMPT_2",
                        log_type="VIDEO",
                        url=url,
                        start_time=row_start_time,
                        video_id=video_id,
                        message="Video ID was found during quick attempt 2."
                    )

            # Final result
            if video_id == "N/A":
                log_step(
                    row_num=row_num,
                    status="NON_VIDEO",
                    log_type="VIDEO",
                    url=url,
                    start_time=row_start_time,
                    video_id="NON_VIDEO",
                    message="No video was detected after attempt 1 and quick attempt 2."
                )

                log_step(
                    row_num=row_num,
                    status="FINISHED",
                    log_type="DEBUG",
                    url=url,
                    start_time=row_start_time,
                    video_id="NON_VIDEO",
                    message="Debug finished. Final result: non-video ad."
                )

                print("\nFinal Result: NON_VIDEO")
                print(f"Total Time: {format_duration(row_start_time)}")
                return

            log_step(
                row_num=row_num,
                status="CHECKING_DETAILS",
                log_type="DETAILS",
                url=url,
                start_time=row_start_time,
                video_id=video_id,
                message="Video found. Now extracting advertiser and ad name."
            )

            advertiser, ad_name = extract_advertiser_and_title(page)

            log_step(
                row_num=row_num,
                status="DETAILS_EXTRACTED",
                log_type="DETAILS",
                url=url,
                start_time=row_start_time,
                video_id=video_id,
                message=f"Advertiser: {advertiser}, Name: {ad_name}"
            )

            log_step(
                row_num=row_num,
                status="FINISHED",
                log_type="DEBUG",
                url=url,
                start_time=row_start_time,
                video_id=video_id,
                message="Debug finished. Final result: video ad."
            )

            print("\nFinal Result:")
            print(f"Video ID: {video_id}")
            print(f"Advertiser: {advertiser}")
            print(f"Name: {ad_name}")
            print(f"Total Time: {format_duration(row_start_time)}")

        except Exception as e:
            log_step(
                row_num=row_num,
                status="ERROR",
                log_type="DEBUG",
                url=url,
                start_time=row_start_time,
                message=f"Debug failed with error: {e}"
            )

            print(f"❌ Debug error: {e}")

        finally:
            flush_logs()

            print("\nBrowser will stay open for 10 seconds so you can inspect it.")
            page.wait_for_timeout(10000)
            page.close()
            context.close()
            browser.close()


if __name__ == "__main__":
    debug_single_link("https://adstransparency.google.com/advertiser/AR04661836496116908033/creative/CR08392446506761715713")