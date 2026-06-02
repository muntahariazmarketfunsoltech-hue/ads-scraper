import gspread
from oauth2client.service_account import ServiceAccountCredentials
import config
import time
from datetime import datetime, timedelta
import uuid

# Cache to reduce repeated API reads
SHEET_CACHE = None
SHEET_CACHE_TIME = None
SHEET_CACHE_TTL = 60  # seconds before refreshing

CLAIM_AGENT_COL = 9
CLAIM_TIME_COL = 10
CLAIM_TOKEN_COL = 11
CLAIM_STATUS_COL = 12
CLAIM_TTL_MINUTES = 5  # for local testing; increase to 370 for production

# --------------------------
# Sheet auth functions
# --------------------------
def get_sheet():
    """Authenticates and returns the Google Sheet worksheet."""
    global SHEET_CACHE, SHEET_CACHE_TIME
    now = time.time()
    if SHEET_CACHE and SHEET_CACHE_TIME and now - SHEET_CACHE_TIME < SHEET_CACHE_TTL:
        return SHEET_CACHE

    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(config.CREDENTIALS_FILE, scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(config.SPREADSHEET_ID).worksheet(config.WORKSHEET_NAME)

    SHEET_CACHE = sheet
    SHEET_CACHE_TIME = now
    return sheet

def get_spreadsheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(config.CREDENTIALS_FILE, scope)
    client = gspread.authorize(creds)
    return client.open_by_key(config.SPREADSHEET_ID)

# --------------------------
# Agent caching functions
# --------------------------
def get_agent_rows_snapshot():
    """
    Reads main sheet once per agent run and caches it to reduce API calls.
    """
    ensure_agent_headers()
    sheet = get_sheet()
    
    # Retry with backoff for 429
    for attempt in range(5):
        try:
            values = sheet.get_all_values()
            break
        except gspread.exceptions.APIError as e:
            if hasattr(e, "response") and e.response.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"⚠ 429 rate limit hit, retrying in {wait}s")
                time.sleep(wait)
            else:
                raise
    else:
        raise Exception("Failed to read sheet after retries due to 429")

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

# --------------------------
# Update get_next_agent_task to use cached rows
# --------------------------
def get_next_agent_task(direction, agent_name, run_id):
    """
    Claims and returns the next row for an agent.
    """
    direction = direction.lower().strip()
    if direction not in ["top", "bottom"]:
        raise ValueError("direction must be 'top' or 'bottom'")

    # Use snapshot instead of reading sheet every time
    rows = get_agent_rows_snapshot()
    unprocessed = [r for r in rows if r["url"] and not r["processed"]]

    if not unprocessed:
        return None

    # Collision rule
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

    candidates = sorted(unprocessed, key=lambda x: x["row_num"], reverse=(direction=="bottom"))

    for candidate in candidates:
        row_num = candidate["row_num"]
        url = candidate["url"]

        if candidate["claim_agent"] and candidate["claim_agent"] != agent_name and not candidate["claim_expired"]:
            continue

        token = f"{agent_name}-{run_id}-{uuid.uuid4().hex[:10]}"
        claim_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Claim the row
        sheet = get_sheet()
        sheet.update(f"I{row_num}:L{row_num}", [[agent_name, claim_time, token, "CLAIMED"]])

        # Confirm claim
        confirm = sheet.row_values(row_num)
        confirmed_token = confirm[10].strip() if len(confirm) >= 11 else ""

        if confirmed_token == token:
            return row_num, url

    return None