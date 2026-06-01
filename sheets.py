import gspread
from oauth2client.service_account import ServiceAccountCredentials
import config
import time
from datetime import datetime


def get_sheet():
    """Authenticates and returns the main Google Sheet worksheet."""
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_name(
        config.CREDENTIALS_FILE,
        scope
    )

    client = gspread.authorize(creds)
    sheet = client.open_by_key(config.SPREADSHEET_ID).worksheet(config.WORKSHEET_NAME)
    return sheet


def get_spreadsheet():
    """Authenticates and returns the full Google Spreadsheet object."""
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_name(
        config.CREDENTIALS_FILE,
        scope
    )

    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(config.SPREADSHEET_ID)
    return spreadsheet


def get_urls():
    """Fetches all transparency URLs from Column H, skipping the header row."""
    sheet = get_sheet()
    records = sheet.col_values(8)  # H

    if len(records) > 1:
        return records[1:]

    return []


def get_urls_with_retry(max_retries=5, delay=5):
    """Fetch transparency URLs with retry protection."""
    for i in range(max_retries):
        try:
            return get_urls()
        except gspread.exceptions.APIError as e:
            print(f"⚠ APIError, retry {i + 1}/{max_retries}: {e}")
            time.sleep(delay)

    raise Exception("Failed to fetch sheet URLs after multiple retries")


def update_combined_row(row_index, data):
    """
    Writes combined video + app-link data into columns A-G.

    Sheet columns:
    A = Advertiser
    B = Name
    C = Ad URL
    D = App Link
    E = App Link Time
    F = Video ID
    G = Video ID Time

    data format:
    [advertiser, ad_name, ad_url, app_link, app_link_time, video_id, video_time]
    """
    sheet = get_sheet()
    cell_range = f"A{row_index}:G{row_index}"
    sheet.update(cell_range, [data])


def update_video_row(row_index, data):
    """
    Optional compatibility function for old video-only scraper.

    Writes video-ad data into columns A-G.

    data format:
    [advertiser, ad_name, ad_url, app_link, app_link_time, video_id, video_time]
    """
    sheet = get_sheet()
    cell_range = f"A{row_index}:G{row_index}"
    sheet.update(cell_range, [data])


def update_app_link(row_index, app_link, app_link_time):
    """
    Optional compatibility function for old app-link-only scraper.

    Writes App Link into column D and App Link Time into column E.
    """
    sheet = get_sheet()
    cell_range = f"D{row_index}:E{row_index}"
    sheet.update(cell_range, [[app_link, app_link_time]])


def get_video_ad_rows():
    """
    Optional compatibility function for old app-link-only scraper.

    Reads transparency URLs from column H and Video IDs from column F.
    Only returns rows where Video ID exists and is not NON_VIDEO/N/A.
    """
    sheet = get_sheet()

    transparency_urls = sheet.col_values(8)  # H
    video_ids = sheet.col_values(6)          # F

    rows = []

    max_len = max(len(transparency_urls), len(video_ids))

    print(f"📌 Column H URL cells found: {len(transparency_urls)}")
    print(f"📌 Column F Video ID cells found: {len(video_ids)}")

    invalid_values = [
        "",
        "N/A",
        "NA",
        "NONE",
        "NULL",
        "NON_VIDEO",
        "VIDEO ID",
        "VIDEO_ID",
        "ERROR"
    ]

    for i in range(1, max_len):
        row_num = i + 1

        url = transparency_urls[i].strip() if i < len(transparency_urls) else ""
        video_id_raw = video_ids[i] if i < len(video_ids) else ""
        video_id = str(video_id_raw).strip()

        if not url:
            print(f"⏭ Row {row_num} skipped: no transparency URL in column H")
            continue

        if video_id.upper() in invalid_values:
            print(f"⏭ Row {row_num} skipped: invalid video ID in column F = '{video_id}'")
            continue

        rows.append((row_num, url))
        print(f"✅ Row {row_num} accepted for app-link scraping")

    print(f"🎬 Total accepted video-ad rows: {len(rows)}")
    return rows


def get_or_create_logs_sheet():
    """
    Gets Logs worksheet. If it does not exist, creates it.

    Logs columns:
    A = Time
    B = Row
    C = Status
    D = Type
    E = URL
    F = Video ID
    G = App Link
    H = Message
    """
    spreadsheet = get_spreadsheet()

    try:
        logs_sheet = spreadsheet.worksheet("Logs")
    except gspread.exceptions.WorksheetNotFound:
        logs_sheet = spreadsheet.add_worksheet(
            title="Logs",
            rows=1000,
            cols=8
        )

        logs_sheet.update(
            "A1:H1",
            [[
                "Time",
                "Row",
                "Status",
                "Type",
                "URL",
                "Video ID",
                "App Link",
                "Message"
            ]]
        )

    return logs_sheet


def add_log(row_number, status, log_type, url="", video_id="", app_link="", message=""):
    """
    Adds one row into Logs worksheet.

    Logs columns:
    Time | Row | Status | Type | URL | Video ID | App Link | Message
    """
    try:
        logs_sheet = get_or_create_logs_sheet()

        log_time = datetime.now().strftime("%I:%M:%S %p")

        logs_sheet.append_row([
            log_time,
            row_number,
            status,
            log_type,
            url,
            video_id,
            app_link,
            message
        ])

    except gspread.exceptions.APIError as e:
        print(f"⚠ Failed to write log due to APIError: {e}")
    except Exception as e:
        print(f"⚠ Failed to write log: {e}")