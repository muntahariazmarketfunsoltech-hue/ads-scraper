import gspread
from oauth2client.service_account import ServiceAccountCredentials
import config
import time
from datetime import datetime, timedelta
import uuid

# ==========================
# CACHE CONFIG
# ==========================
SHEET_CACHE = None
SHEET_CACHE_TIME = 0
SHEET_CACHE_TTL = 60

SNAPSHOT_CACHE = None
SNAPSHOT_TIME = 0
SNAPSHOT_TTL = 10  # IMPORTANT: reduces API hits massively

# ==========================
# COLUMNS
# ==========================
CLAIM_AGENT_COL = 9
CLAIM_TIME_COL = 10
CLAIM_TOKEN_COL = 11
CLAIM_STATUS_COL = 12

# Safer for long scraper runs.
# If your run can take around 5 hours, 5 minutes is too short.
CLAIM_TTL_MINUTES = 355

LOG_CACHE = []
WRITE_LOGS = False


# ==========================
# SHEET AUTH
# ==========================
def get_sheet():
    global SHEET_CACHE, SHEET_CACHE_TIME

    now = time.time()
    if SHEET_CACHE and (now - SHEET_CACHE_TIME) < SHEET_CACHE_TTL:
        return SHEET_CACHE

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]

    creds = ServiceAccountCredentials.from_json_keyfile_name(
        config.CREDENTIALS_FILE, scope
    )

    client = gspread.authorize(creds)
    sheet = client.open_by_key(config.SPREADSHEET_ID).worksheet(config.WORKSHEET_NAME)

    SHEET_CACHE = sheet
    SHEET_CACHE_TIME = now
    return sheet


# --------------------------
# Logs disabled
# --------------------------
WRITE_LOGS = False


def flush_logs():
    """Logs disabled - do nothing"""
    global LOG_CACHE
    LOG_CACHE = []
    return


def add_log(row_number="", status="", log_type="", url="", video_id="", app_link="", message=""):
    """Logs disabled - do nothing"""
    return


# ==========================
# HELPERS
# ==========================
def is_claim_expired(claim_time_text):
    if not claim_time_text:
        return True
    try:
        t = datetime.strptime(claim_time_text, "%Y-%m-%d %H:%M:%S")
        return datetime.now() - t > timedelta(minutes=CLAIM_TTL_MINUTES)
    except Exception:
        return True


BAD_DONE_VALUES = {"", "N/A", "NA", "ERROR", "NOT FOUND", "NONE", "NULL", "#N/A"}


def _clean_cell(value):
    return str(value or "").strip()


def is_good_done_value(value):
    value = _clean_cell(value)
    return bool(value) and value.upper() not in BAD_DONE_VALUES


def row_has_done_output(row):
    """
    Row is treated as DONE if:
    - Column L says DONE, OR
    - important output columns already have good data.

    This protects rows even if Column F is blank/removed.
    """
    claim_status = _clean_cell(row[11]) if len(row) > 11 else ""

    if claim_status.upper() == "DONE":
        return True

    package_name = _clean_cell(row[1]) if len(row) > 1 else ""     # B
    app_link = _clean_cell(row[3]) if len(row) > 3 else ""         # D
    video_marker = _clean_cell(row[5]) if len(row) > 5 else ""     # F
    headline = _clean_cell(row[12]) if len(row) > 12 else ""       # M
    description = _clean_cell(row[13]) if len(row) > 13 else ""    # N

    return any([
        is_good_done_value(package_name),
        is_good_done_value(app_link),
        is_good_done_value(video_marker),
        is_good_done_value(headline),
        is_good_done_value(description),
    ])


def output_data_is_success(data):
    """
    data[5] is Column F:
    - video id for video ads
    - text/image for non-video ads
    """
    if not data or len(data) < 6:
        return False

    return is_good_done_value(data[5])


def _strip_region(url):
    url = _clean_cell(url)
    url = url.replace("&region=anywhere", "")
    url = url.replace("?region=anywhere", "")
    return url.rstrip("?&")


def row_url_matches(existing_row, scrape_url):
    """
    Before writing, confirm current sheet row's Column H URL
    matches the URL being scraped.
    """
    sheet_url = _clean_cell(existing_row[7]) if len(existing_row) > 7 else ""
    scrape_url = _clean_cell(scrape_url)

    if not sheet_url or not scrape_url:
        return False

    sheet_url_clean = _strip_region(sheet_url)
    scrape_url_clean = _strip_region(scrape_url)

    return (
        sheet_url_clean == scrape_url_clean
        or sheet_url_clean in scrape_url_clean
        or scrape_url_clean in sheet_url_clean
    )


# ==========================
# SNAPSHOT (CRITICAL OPTIMIZATION)
# ==========================
def get_agent_rows_snapshot():
    """
    ONE FULL READ ONLY (cached for 10 seconds)
    """
    global SNAPSHOT_CACHE, SNAPSHOT_TIME

    now = time.time()
    if SNAPSHOT_CACHE and (now - SNAPSHOT_TIME) < SNAPSHOT_TTL:
        return SNAPSHOT_CACHE

    sheet = get_sheet()

    for attempt in range(5):
        try:
            values = sheet.get_all_values()
            break
        except gspread.exceptions.APIError as e:
            if "429" in str(e):
                wait = 2 * (attempt + 1)
                print(f"⚠ 429 hit, retrying in {wait}s")
                time.sleep(wait)
            else:
                raise
    else:
        raise Exception("Failed to read sheet after retries")

    rows = []

    for idx in range(1, len(values)):
        row = values[idx]
        row_num = idx + 1

        url = row[7].strip() if len(row) > 7 else ""
        video_id = row[5].strip() if len(row) > 5 else ""

        claim_agent = row[8].strip() if len(row) > 8 else ""
        claim_time = row[9].strip() if len(row) > 9 else ""
        claim_token = row[10].strip() if len(row) > 10 else ""
        claim_status = row[11].strip() if len(row) > 11 else ""

        # Stop flag disabled. Column M is not used as STOP anymore.
        # Keeping the key for compatibility with your existing get_next_agent_task logic.
        stop_flag = ""

        rows.append({
            "row_num": row_num,
            "url": url,
            "video_id": video_id,
            "claim_agent": claim_agent,
            "claim_time": claim_time,
            "claim_token": claim_token,
            "claim_status": claim_status,
            "stop_flag": stop_flag,
            "processed": row_has_done_output(row),
            "claim_expired": is_claim_expired(claim_time)
        })

    SNAPSHOT_CACHE = rows
    SNAPSHOT_TIME = now

    return rows


# ==========================
# CORE TASK PICKER (FIXED)
# ==========================
def get_next_agent_task(direction, agent_name, run_id):
    direction = direction.lower().strip()

    if direction not in ["top", "bottom"]:
        raise ValueError("direction must be top or bottom")

    sheet = get_sheet()
    rows = get_agent_rows_snapshot()

    unprocessed = [r for r in rows if r["url"] and not r["processed"]]

    if not unprocessed:
        return None

    # collision protection
    if len(unprocessed) == 1 and direction == "bottom":
        return "COLLISION_STOP"

    candidates = sorted(
        unprocessed,
        key=lambda x: x["row_num"],
        reverse=(direction == "bottom")
    )

    for c in candidates:
        row_num = c["row_num"]

        # This stays, but stop_flag is always empty now.
        if c["stop_flag"].upper() == "STOP":
            return "COLLISION_STOP"

        # skip active claims
        if c["claim_agent"] and c["claim_agent"] != agent_name and not c["claim_expired"]:
            continue

        token = f"{agent_name}-{run_id}-{uuid.uuid4().hex[:10]}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # SINGLE WRITE ONLY (claim row)
        sheet.update(
            f"I{row_num}:L{row_num}",
            [[agent_name, now, token, "CLAIMED"]]
        )

        # ❌ REMOVED: confirm read (major quota fix)
        # We trust write success instead of re-reading sheet

        return row_num, c["url"]

    return None


# ==========================
# SIMPLE STATUS UPDATE
# ==========================
def mark_agent_done(row_num, agent_name=None):
    sheet = get_sheet()
    try:
        sheet.update_cell(row_num, CLAIM_STATUS_COL, "DONE")
    except Exception:
        pass


# ==========================
# BULK UPDATE HELPERS
# ==========================
def update_combined_row(row_index, data):
    """
    Protected A:G update.
    - Does not overwrite already completed rows.
    - Does not write when the current row URL does not match the scraped URL.
    - Marks Column L as DONE on successful output.
    """
    sheet = get_sheet()

    try:
        existing_row = sheet.row_values(row_index)

        # Do not overwrite already completed rows.
        if row_has_done_output(existing_row):
            print(f"⏭ Row {row_index}: already DONE, skipping A:G overwrite")
            return False

        # Safety check: do not write if row URL does not match scraped URL.
        scrape_url = data[2] if len(data) > 2 else ""

        if not row_url_matches(existing_row, scrape_url):
            print(f"⚠ Row {row_index}: URL mismatch, skipping A:G write")
            return False

        sheet.update(f"A{row_index}:G{row_index}", [data])

        # Mark successful rows as DONE in Column L.
        if output_data_is_success(data):
            try:
                sheet.update_cell(row_index, CLAIM_STATUS_COL, "DONE")
            except Exception:
                pass

        return True

    except Exception as e:
        print(f"Update error: {e}")
        return False


def update_headline_and_description(row_index, headline, description):
    """
    Protected M:N update.
    - Does not overwrite existing good headline/description.
    """
    sheet = get_sheet()

    try:
        existing = sheet.get(f"M{row_index}:N{row_index}")

        old_headline = ""
        old_description = ""

        if existing and len(existing) > 0:
            old_headline = existing[0][0] if len(existing[0]) > 0 else ""
            old_description = existing[0][1] if len(existing[0]) > 1 else ""

        # Do not overwrite existing good headline/description.
        if is_good_done_value(old_headline) or is_good_done_value(old_description):
            print(f"⏭ Row {row_index}: M:N already has data, skipping overwrite")
            return False

        sheet.update(f"M{row_index}:N{row_index}", [[headline, description]])
        return True

    except Exception as e:
        print(f"Update error: {e}")
        return False


# ==========================
# OPTIMIZED URL FETCH (NO EXTRA SNAPSHOT CALL)
# ==========================
def get_urls_with_retry():
    rows = get_agent_rows_snapshot()
    return [
        (r["row_num"], r["url"])
        for r in rows
        if r["url"] and not r["processed"]
    ]


# ==========================
# OPTIONAL UTILS
# ==========================
def count_unprocessed_rows():
    rows = get_agent_rows_snapshot()
    return sum(1 for r in rows if r["url"] and not r["processed"])
