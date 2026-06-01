import gspread
from oauth2client.service_account import ServiceAccountCredentials
import config
import time


def get_sheet():
    """Authenticates and returns the Google Sheet object."""
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


def get_urls():
    """Fetches all transparency URLs from Column H, skipping the header row."""
    sheet = get_sheet()
    records = sheet.col_values(8)

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


def update_video_row(row_index, data):
    """
    Writes video-ad data into columns A-E.

    data format:
    [advertiser, ad_name, ad_url, app_link, video_id]

    App Link is blank here.
    """
    sheet = get_sheet()
    cell_range = f"A{row_index}:E{row_index}"
    sheet.update(cell_range, [data])


def update_app_link(row_index, app_link):
    """
    Writes only App Link into column D.
    """
    sheet = get_sheet()
    sheet.update_cell(row_index, 4, app_link)


def get_video_ad_rows():
    """
    Reads transparency URLs from column H and Video IDs from column E.
    Only returns rows where Video ID exists.

    This prevents app links from being saved for non-video ads.
    """
    sheet = get_sheet()

    transparency_urls = sheet.col_values(8)  # H
    video_ids = sheet.col_values(5)          # E

    rows = []

    max_len = max(len(transparency_urls), len(video_ids))

    for i in range(1, max_len):
        row_num = i + 1

        url = transparency_urls[i].strip() if i < len(transparency_urls) else ""
        video_id = video_ids[i].strip() if i < len(video_ids) else ""

        if not url:
            continue

        if not video_id or video_id.upper() == "N/A":
            print(f"⏭ Row {row_num} skipped: no video ID in column E")
            continue

        rows.append((row_num, url))

    return rows