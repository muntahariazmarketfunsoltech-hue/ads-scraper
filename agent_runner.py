import sys
import time
import uuid
from datetime import datetime

import sheets
from scraper import scrape_single_url

MAX_RUNTIME_SECONDS = (5 * 60 * 60) + (50 * 60)  # 5h50m


def now_text():
    return datetime.now().strftime("%I:%M:%S %p")


def claim_row(row_num, agent_name, run_id):
    """
    Fast claim write only.
    Does NOT call get_all_values().
    Uses I:L:
    I = claim agent
    J = claim time
    K = claim token
    L = claim status
    """
    sheet = sheets.get_sheet()
    token = f"{agent_name}-{run_id}-{uuid.uuid4().hex[:10]}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    sheet.update(
        f"I{row_num}:L{row_num}",
        [[agent_name, now, token, "CLAIMED"]]
    )


def get_fast_agent_tasks(direction):
    """
    Fetch pending rows once from sheets.get_urls_with_retry().
    Requires updated sheets.py where get_urls_with_retry() returns:
        [(row_num, url), ...]
    """
    tasks = sheets.get_urls_with_retry()

    if not tasks:
        return []

    # Safety: stop if old sheets.py is still returning only URLs.
    first = tasks[0]
    if not isinstance(first, (tuple, list)) or len(first) != 2:
        raise RuntimeError(
            "Your sheets.py is still using old get_urls_with_retry(). "
            "It must return [(row_num, url), ...], not only URLs."
        )

    # Ensure proper row order.
    tasks = [(int(row_num), str(url).strip()) for row_num, url in tasks if url and str(url).strip()]
    tasks.sort(key=lambda x: x[0])

    # When running TOP and BOTTOM together, split the pending rows in half.
    # This avoids both agents selecting the same middle rows from the same snapshot.
    mid = (len(tasks) + 1) // 2

    if direction == "top":
        return tasks[:mid]

    # Bottom starts from bottom side of second half.
    return list(reversed(tasks[mid:]))


def run_agent(direction):
    direction = direction.lower().strip()
    if direction not in ["top", "bottom"]:
        raise ValueError("Use: python agent_runner.py top OR python agent_runner.py bottom")

    agent_name = f"AGENT_{direction.upper()}"
    run_id = uuid.uuid4().hex[:8]

    start_time = time.time()
    deadline = start_time + MAX_RUNTIME_SECONDS
    processed_count = 0

    print(f"🚀 {agent_name} started at {now_text()} with run_id={run_id}")

    sheets.add_log(
        row_number="",
        status="AGENT_STARTED",
        log_type=agent_name,
        message=f"{agent_name} started with run_id={run_id}"
    )

    # Fetch pending row list once.
    # This avoids calling get_all_values() before every single row.
    tasks = get_fast_agent_tasks(direction)

    if not tasks:
        print(f"✅ {agent_name}: no pending rows found.")
        sheets.add_log(
            row_number="",
            status="NO_ROWS_LEFT",
            log_type=agent_name,
            message="No pending rows found"
        )
        sheets.flush_logs()
        return

    print(f"📌 {agent_name}: loaded {len(tasks)} pending rows for this run")

    for row_num, url in tasks:
        if time.time() >= deadline:
            break

        try:
            claim_row(row_num, agent_name, run_id)
            print(f"🔒 {agent_name}: claimed row {row_num}")

            sheets.add_log(
                row_number=row_num,
                status="ROW_CLAIMED",
                log_type=agent_name,
                url=url,
                message=f"{agent_name} claimed row {row_num}"
            )

            row_start = time.time()

            scrape_single_url((row_num, url))

            sheets.mark_agent_done(row_num, agent_name)
            processed_count += 1

            print(
                f"✅ {agent_name}: finished row {row_num} "
                f"in {round(time.time() - row_start, 1)}s"
            )

        except Exception as e:
            print(f"❌ {agent_name}: error row {row_num}: {e}")
            sheets.add_log(
                row_number=row_num,
                status="AGENT_ROW_ERROR",
                log_type=agent_name,
                url=url,
                message=str(e)
            )

        time.sleep(2)  # pause to avoid hitting Sheets API too fast

    sheets.add_log(
        row_number="",
        status="AGENT_STOPPED",
        log_type=agent_name,
        message=f"{agent_name} stopped. Processed rows: {processed_count}"
    )
    sheets.flush_logs()

    print(f"🛑 {agent_name} stopped at {now_text()}. Processed rows: {processed_count}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python agent_runner.py top")
        print("Usage: python agent_runner.py bottom")
        sys.exit(1)

    run_agent(sys.argv[1])
