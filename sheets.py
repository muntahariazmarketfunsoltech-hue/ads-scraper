import gspread
from oauth2client.service_account import ServiceAccountCredentials
import config
import time
from datetime import datetime
import uuid


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
        import uuid
from datetime import datetime, timedelta


CLAIM_AGENT_COL = 9      # I
CLAIM_TIME_COL = 10      # J
CLAIM_TOKEN_COL = 11     # K
CLAIM_STATUS_COL = 12    # L

CLAIM_TTL_MINUTES = 5    # local testing; later change to 370


def ensure_agent_headers():
    """
    Adds internal agent columns:
    I = Agent
    J = Claim Time
    K = Claim Token
    L = Claim Status
    """
    sheet = get_sheet()

    headers = sheet.row_values(1)

    required = {
        9: "Agent",
        10: "Claim Time",
        11: "Claim Token",
        12: "Claim Status"
    }

    updates = []

    for col, name in required.items():
        current = headers[col - 1] if len(headers) >= col else ""

        if current != name:
            col_letter = chr(64 + col)
            updates.append({
                "range": f"{col_letter}1",
                "values": [[name]]
            })

    if updates:
        sheet.batch_update(updates)


def is_claim_expired(claim_time_text):
    """
    Returns True if claim is old/stale.
    """
    if not claim_time_text:
        return True

    try:
        claim_time = datetime.strptime(claim_time_text, "%Y-%m-%d %H:%M:%S")
        return datetime.now() - claim_time > timedelta(minutes=CLAIM_TTL_MINUTES)
    except Exception:
        return True


def is_processed_video_value(value):
    """
    Column F decides whether row is already processed.
    Any real value means do not overwrite.
    """
    value = str(value or "").strip()

    if not value:
        return False

    return True


def get_agent_rows_snapshot():
    """
    Reads main sheet once and returns row info.

    H = transparency_url
    F = Video ID / NON_VIDEO / ERROR
    I-L = internal claim columns
    """
    ensure_agent_headers()

    sheet = get_sheet()
    values = sheet.get_all_values()

    rows = []

    for idx in range(1, len(values)):
        row_num = idx + 1
        row = values[idx]

        url = row[7].strip() if len(row) >= 8 else ""
        video_id = row[5].strip() if len(row) >= 6 else ""

        claim_agent = row[8].strip() if len(row) >= 9 else ""
        claim_time = row[9].strip() if len(row) >= 10 else ""
        claim_token = row[10].strip() if len(row) >= 11 else ""
        claim_status = row[11].strip() if len(row) >= 12 else ""

        if not url:
            continue

        rows.append({
            "row_num": row_num,
            "url": url,
            "video_id": video_id,
            "claim_agent": claim_agent,
            "claim_time": claim_time,
            "claim_token": claim_token,
            "claim_status": claim_status,
            "processed": is_processed_video_value(video_id),
            "claim_expired": is_claim_expired(claim_time)
        })

    return rows


def count_unprocessed_rows():
    """
    Counts rows with URL in column H and blank column F.
    """
    rows = get_agent_rows_snapshot()

    count = 0

    for row in rows:
        if row["url"] and not row["processed"]:
            count += 1

    return count


def get_next_agent_task(direction, agent_name, run_id):
    """
    Claims and returns the next row for an agent.

    direction:
    - top = starts from top
    - bottom = starts from bottom

    Collision rule:
    - If only one row is left, bottom stops and top processes it.
    - If row is already claimed by another active agent, skip it.
    - Claim is confirmed by writing token then reading it back.
    """
    direction = direction.lower().strip()

    if direction not in ["top", "bottom"]:
        raise ValueError("direction must be 'top' or 'bottom'")

    sheet = get_sheet()
    rows = get_agent_rows_snapshot()

    unprocessed = [
        r for r in rows
        if r["url"] and not r["processed"]
    ]

    if not unprocessed:
        return None

    # If only one row remains, bottom stops so both agents do not collide.
    if len(unprocessed) == 1 and direction == "bottom":
        try:
            add_log(
                row_number="",
                status="COLLISION_STOP",
                log_type=agent_name,
                message="Only one unprocessed row left. Bottom agent stopped to avoid collision."
            )
        except Exception:
            pass

        return "COLLISION_STOP"

    if direction == "top":
        candidates = sorted(unprocessed, key=lambda x: x["row_num"])
    else:
        candidates = sorted(unprocessed, key=lambda x: x["row_num"], reverse=True)

    for candidate in candidates:
        row_num = candidate["row_num"]
        url = candidate["url"]

        # Skip active claims from another agent.
        if (
            candidate["claim_agent"]
            and candidate["claim_agent"] != agent_name
            and not candidate["claim_expired"]
        ):
            continue

        token = f"{agent_name}-{run_id}-{uuid.uuid4().hex[:10]}"
        claim_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Claim row.
        sheet.update(
            f"I{row_num}:L{row_num}",
            [[agent_name, claim_time, token, "CLAIMED"]]
        )

        # Confirm claim.
        confirm = sheet.row_values(row_num)
        confirmed_token = confirm[10].strip() if len(confirm) >= 11 else ""

        if confirmed_token == token:
            return row_num, url

    return None


def mark_agent_done(row_num, agent_name):
    """
    Marks claim as completed after scraper finishes the row.
    """
    try:
        sheet = get_sheet()
        sheet.update_cell(row_num, CLAIM_STATUS_COL, "DONE")
    except Exception:
        pass