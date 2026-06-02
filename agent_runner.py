import sys
import time
import uuid
from datetime import datetime

import sheets
from scraper import scrape_single_url
import gspread

MAX_RUNTIME_SECONDS = (5 * 60 * 60) + (50 * 60)  # 5 hours 50 minutes
SHEET_FETCH_RETRY = 5  # retries for 429

def now_text():
    return datetime.now().strftime("%I:%M:%S %p")

def fetch_sheet_once_with_retry():
    """
    Fetch the sheet rows once with retries on 429 quota errors.
    """
    for attempt in range(SHEET_FETCH_RETRY):
        try:
            return sheets.get_agent_rows_snapshot()
        except gspread.exceptions.APIError as e:
            if hasattr(e, 'response') and e.response.status_code == 429:
                wait = 5 * (attempt + 1)
                print(f"⚠ 429 rate limit, retrying in {wait}s")
                time.sleep(wait)
            else:
                raise
    raise Exception("Failed to fetch sheet after multiple 429 retries")

def run_agent(direction):
    direction = direction.lower().strip()
    if direction not in ["top", "bottom"]:
        raise ValueError("Use: python agent_runner.py top OR python agent_runner.py bottom")

    agent_name = f"AGENT_{direction.upper()}"
    run_id = uuid.uuid4().hex[:8]

    start_time = time.time()
    deadline = start_time + MAX_RUNTIME_SECONDS

    print(f"🚀 {agent_name} started at {now_text()} with run_id={run_id}")
    sheets.add_log(
        row_number="",
        status="AGENT_STARTED",
        log_type=agent_name,
        message=f"{agent_name} started with run_id={run_id}"
    )

    processed_count = 0

    # Fetch sheet once at start
    rows_snapshot = fetch_sheet_once_with_retry()

    while time.time() < deadline:
        remaining_seconds = int(deadline - time.time())
        if remaining_seconds <= 0:
            break

        # Claim next row
        task = sheets.get_next_agent_task(
            direction=direction,
            agent_name=agent_name,
            run_id=run_id
        )

        if task is None:
            print(f"✅ {agent_name}: no unprocessed rows left.")
            sheets.add_log(
                row_number="",
                status="NO_ROWS_LEFT",
                log_type=agent_name,
                message="No unprocessed rows left"
            )
            break

        if task == "COLLISION_STOP":
            print(f"🛑 {agent_name}: stopped to avoid collision.")
            break

        row_num, url = task
        print(f"🔒 {agent_name}: claimed row {row_num}")
        sheets.add_log(
            row_number=row_num,
            status="ROW_CLAIMED",
            log_type=agent_name,
            url=url,
            message=f"{agent_name} claimed row {row_num}"
        )

        try:
            # Scrape URL
            scrape_single_url((row_num, url))
            sheets.mark_agent_done(row_num, agent_name)
            processed_count += 1
            print(f"✅ {agent_name}: finished row {row_num}")
        except Exception as e:
            print(f"❌ {agent_name}: error row {row_num}: {e}")
            sheets.add_log(
                row_number=row_num,
                status="AGENT_ROW_ERROR",
                log_type=agent_name,
                url=url,
                message=str(e)
            )

        # small pause to reduce API reads
        time.sleep(2)

    sheets.add_log(
        row_number="",
        status="AGENT_STOPPED",
        log_type=agent_name,
        message=f"{agent_name} stopped. Processed rows: {processed_count}"
    )
    print(f"🛑 {agent_name} stopped at {now_text()}. Processed rows: {processed_count}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python agent_runner.py top")
        print("Usage: python agent_runner.py bottom")
        sys.exit(1)

    run_agent(sys.argv[1])