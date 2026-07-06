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
SNAPSHOT_TTL = 10  # reduces API hits

# ==========================
# COLUMNS
# ==========================
CLAIM_AGENT_COL = 9       # I
CLAIM_TIME_COL = 10       # J
CLAIM_TOKEN_COL = 11      # K
CLAIM_STATUS_COL = 12     # L
CLAIM_TTL_MINUTES = 355   # safer for long 5h+ runs

LOG_CACHE = []
WRITE_LOGS = False

# ==========================
# FAST ROW SAFETY CACHE
# ==========================
# Filled by get_agent_rows_snapshot().
# Also manually filled by register_row_url() for child-process scraping.
ROW_URL_CACHE = {}
DONE_ROW_CACHE = set()


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


# ==========================
# LOGS DISABLED
# ==========================
def flush_logs():
    """Logs disabled - do nothing"""
    global LOG_CACHE
    LOG_CACHE = []
    return


def add_log(row_number="", status="", log_type="", url="", video_id="", app_link="", message=""):
    """Logs disabled - do nothing"""
    return


# ==========================
# RETRY HELPERS
# ==========================
RETRYABLE_CODES = ["429", "500", "502", "503", "504"]


def _is_retryable_api_error(err):
    text = str(err)
    return any(code in text for code in RETRYABLE_CODES)


def _retry_sleep(attempt, base_seconds=5):
    wait = base_seconds * (attempt + 1)
    print(f"⏳ Retry wait {wait}s", flush=True)
    time.sleep(wait)


def safe_update_range(sheet, range_name, values, retries=8):
    """
    Uses named arguments to avoid gspread deprecation warning.
    Retries temporary Google Sheets API errors.
    """
    for attempt in range(retries):
        try:
            sheet.update(range_name=range_name, values=values)
            return True
        except gspread.exceptions.APIError as e:
            if _is_retryable_api_error(e) and attempt < retries - 1:
                print(f"⚠ Sheets update error on {range_name}, retrying: {e}", flush=True)
                _retry_sleep(attempt)
                continue
            raise
    return False


def safe_update_cell(sheet, row, col, value, retries=8):
    for attempt in range(retries):
        try:
            sheet.update_cell(row, col, value)
            return True
        except gspread.exceptions.APIError as e:
            if _is_retryable_api_error(e) and attempt < retries - 1:
                print(f"⚠ Sheets cell update error R{row}C{col}, retrying: {e}", flush=True)
                _retry_sleep(attempt)
                continue
            raise
    return False


def get_all_values_with_retry(sheet, retries=10):
    for attempt in range(retries):
        try:
            return sheet.get_all_values()
        except gspread.exceptions.APIError as e:
            if _is_retryable_api_error(e) and attempt < retries - 1:
                print(f"⚠ Sheets read error, retrying: {e}", flush=True)
                _retry_sleep(attempt, base_seconds=10)
                continue
            raise
    raise Exception("Failed to read sheet after retries")


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
    Row is DONE if:
    - Column L says DONE, OR
    - important output columns already have good data.

    This protects rows even if Column F becomes blank/removed.
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


def url_text_matches(sheet_url, scrape_url):
    sheet_url = _clean_cell(sheet_url)
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


def register_row_url(row_num, url):
    """
    Fast child-process safety:
    lets scraper update one row without calling get_all_values()
    in child processes.
    """
    try:
        ROW_URL_CACHE[int(row_num)] = _clean_cell(url)
    except Exception:
        pass


# ==========================
# SNAPSHOT
# ==========================
def get_agent_rows_snapshot():
    """
    ONE FULL READ ONLY, cached.
    Also fills ROW_URL_CACHE and DONE_ROW_CACHE for fast write protection.
    """
    global SNAPSHOT_CACHE, SNAPSHOT_TIME, ROW_URL_CACHE, DONE_ROW_CACHE

    now = time.time()
    if SNAPSHOT_CACHE and (now - SNAPSHOT_TIME) < SNAPSHOT_TTL:
        return SNAPSHOT_CACHE

    sheet = get_sheet()
    values = get_all_values_with_retry(sheet)

    rows = []
    row_url_cache = {}
    done_row_cache = set()

    for idx in range(1, len(values)):  # skip header
        row = values[idx]
        row_num = idx + 1

        url = row[7].strip() if len(row) > 7 else ""
        video_id = row[5].strip() if len(row) > 5 else ""

        claim_agent = row[8].strip() if len(row) > 8 else ""
        claim_time = row[9].strip() if len(row) > 9 else ""
        claim_token = row[10].strip() if len(row) > 10 else ""
        claim_status = row[11].strip() if len(row) > 11 else ""

        # Stop flag disabled. Column M is headline, not STOP.
        stop_flag = ""

        processed = row_has_done_output(row)

        row_url_cache[row_num] = url
        if processed:
            done_row_cache.add(row_num)

        rows.append({
            "row_num": row_num,
            "url": url,
            "video_id": video_id,
            "claim_agent": claim_agent,
            "claim_time": claim_time,
            "claim_token": claim_token,
            "claim_status": claim_status,
            "stop_flag": stop_flag,
            "processed": processed,
            "claim_expired": is_claim_expired(claim_time)
        })

    ROW_URL_CACHE = row_url_cache
    DONE_ROW_CACHE = done_row_cache

    SNAPSHOT_CACHE = rows
    SNAPSHOT_TIME = now

    return rows


# ==========================
# CORE TASK PICKER
# Kept for compatibility, but fast agent_runner does not use this per row.
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

    if len(unprocessed) == 1 and direction == "bottom":
        return "COLLISION_STOP"

    candidates = sorted(
        unprocessed,
        key=lambda x: x["row_num"],
        reverse=(direction == "bottom")
    )

    for c in candidates:
        row_num = c["row_num"]

        # stop_flag is always empty now.
        if c["stop_flag"].upper() == "STOP":
            return "COLLISION_STOP"

        # skip active claims
        if c["claim_agent"] and c["claim_agent"] != agent_name and not c["claim_expired"]:
            continue

        token = f"{agent_name}-{run_id}-{uuid.uuid4().hex[:10]}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        safe_update_range(
            sheet,
            range_name=f"I{row_num}:L{row_num}",
            values=[[agent_name, now, token, "CLAIMED"]]
        )

        return row_num, c["url"]

    return None


# ==========================
# STATUS UPDATE
# ==========================
def mark_agent_done(row_num, agent_name=None):
    sheet = get_sheet()
    try:
        safe_update_cell(sheet, row_num, CLAIM_STATUS_COL, "DONE")
    except Exception as e:
        print(f"⚠ Could not mark row {row_num} DONE: {e}", flush=True)


def mark_agent_timeout(row_num):
    sheet = get_sheet()
    try:
        safe_update_cell(sheet, row_num, CLAIM_STATUS_COL, "TIMEOUT")
    except Exception as e:
        print(f"⚠ Could not mark row {row_num} TIMEOUT: {e}", flush=True)


# ==========================
# UPDATE HELPERS
# ==========================
def update_combined_row(row_index, data):
    """
    FAST protected A:G update.
    No per-row sheet read.
    """
    global DONE_ROW_CACHE

    sheet = get_sheet()

    try:
        # Make sure URL cache exists. In child process, scraper calls register_row_url().
        if row_index not in ROW_URL_CACHE:
            print(f"⚠ Row {row_index}: URL cache missing, skipping A:G write to avoid overwrite", flush=True)
            return False

        if row_index in DONE_ROW_CACHE:
            print(f"⏭ Row {row_index}: already DONE, skipping A:G overwrite", flush=True)
            return False

        scrape_url = data[2] if len(data) > 2 else ""
        sheet_url = ROW_URL_CACHE.get(row_index, "")

        if not url_text_matches(sheet_url, scrape_url):
            print(f"⚠ Row {row_index}: URL mismatch, skipping A:G write", flush=True)
            return False

        safe_update_range(
            sheet,
            range_name=f"A{row_index}:G{row_index}",
            values=[data]
        )

        if output_data_is_success(data):
            DONE_ROW_CACHE.add(row_index)
            try:
                safe_update_cell(sheet, row_index, CLAIM_STATUS_COL, "DONE")
            except Exception:
                pass

        return True

    except Exception as e:
        print(f"Update error on A:G row {row_index}: {e}", flush=True)
        return False


def update_headline_and_description(row_index, headline, description):
    """
    FAST M:N update.
    This is only called after A:G write succeeds.
    """
    sheet = get_sheet()

    try:
        safe_update_range(
            sheet,
            range_name=f"M{row_index}:N{row_index}",
            values=[[headline, description]]
        )
        return True

    except Exception as e:
        print(f"Update error on M:N row {row_index}: {e}", flush=True)
        return False


# ==========================
# URL FETCH
# ==========================
def get_urls_with_retry():
    rows = get_agent_rows_snapshot()
    return [
        (r["row_num"], r["url"])
        for r in rows
        if r["url"] and not r["processed"]
    ]


def count_unprocessed_rows():
    rows = get_agent_rows_snapshot()
    return sum(1 for r in rows if r["url"] and not r["processed"])
