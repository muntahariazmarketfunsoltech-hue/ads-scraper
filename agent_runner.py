import sys
import time
import uuid
import multiprocessing
from datetime import datetime

import sheets
from scraper import scrape_single_url

MAX_RUNTIME_SECONDS = (5 * 60 * 60) + (50 * 60)  # 5h50m

# If one row gets stuck, skip it and continue.
# 240 seconds = 4 minutes. Change to 300 if you want more time per row.
ROW_TIMEOUT_SECONDS = 240


def now_text():
    return datetime.now().strftime("%I:%M:%S %p")


def claim_row(row_num, agent_name, run_id):
    """
    Fast claim write only.
    Uses I:L:
    I = claim agent
    J = claim time
    K = claim token
    L = claim status
    """
    sheet = sheets.get_sheet()
    token = f"{agent_name}-{run_id}-{uuid.uuid4().hex[:10]}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"⏳ {agent_name}: claiming row {row_num}", flush=True)

    sheets.safe_update_range(
        sheet,
        range_name=f"I{row_num}:L{row_num}",
        values=[[agent_name, now, token, "CLAIMED"]]
    )

    print(f"🔒 {agent_name}: claimed row {row_num}", flush=True)


def get_fast_agent_tasks(direction):
    """
    Fetch pending rows once from sheets.get_urls_with_retry().
    Requires sheets.py where get_urls_with_retry() returns:
        [(row_num, url), ...]
    """
    print(f"📥 Loading pending rows snapshot...", flush=True)
    tasks = sheets.get_urls_with_retry()

    if not tasks:
        return []

    first = tasks[0]
    if not isinstance(first, (tuple, list)) or len(first) != 2:
        raise RuntimeError(
            "Your sheets.py is still old. get_urls_with_retry() must return "
            "[(row_num, url), ...], not only URLs."
        )

    tasks = [(int(row_num), str(url).strip()) for row_num, url in tasks if url and str(url).strip()]
    tasks.sort(key=lambda x: x[0])

    # Top and bottom split the pending rows from the same snapshot.
    mid = (len(tasks) + 1) // 2

    if direction == "top":
        return tasks[:mid]

    return list(reversed(tasks[mid:]))


def _scrape_child(row_num, url, queue):
    """
    Child process wrapper.
    Important: register the row URL in child process so sheets.py can validate
    row mapping without reading the full sheet again.
    """
    try:
        sheets.register_row_url(row_num, url)
        scrape_single_url((row_num, url))
        queue.put(("OK", ""))
    except Exception as e:
        queue.put(("ERROR", str(e)))


def scrape_with_timeout(row_num, url, timeout_seconds=ROW_TIMEOUT_SECONDS):
    """
    Runs scrape_single_url in a child process.
    If Playwright/browser hangs, parent terminates it and continues next row.
    """
    queue = multiprocessing.Queue()
    proc = multiprocessing.Process(target=_scrape_child, args=(row_num, url, queue))

    proc.start()
    proc.join(timeout_seconds)

    if proc.is_alive():
        print(f"⏱ Row {row_num}: timeout after {timeout_seconds}s, terminating process", flush=True)
        proc.terminate()
        proc.join(15)

        if proc.is_alive():
            proc.kill()
            proc.join(5)

        return False, "TIMEOUT"

    if not queue.empty():
        status, message = queue.get()
        if status == "OK":
            return True, ""
        return False, message

    if proc.exitcode == 0:
        return True, ""

    return False, f"Child process exited with code {proc.exitcode}"


def run_agent(direction):
    direction = direction.lower().strip()
    if direction not in ["top", "bottom"]:
        raise ValueError("Use: python agent_runner.py top OR python agent_runner.py bottom")

    agent_name = f"AGENT_{direction.upper()}"
    run_id = uuid.uuid4().hex[:8]

    start_time = time.time()
    deadline = start_time + MAX_RUNTIME_SECONDS

    processed_count = 0
    timeout_count = 0
    error_count = 0

    print(f"🚀 {agent_name} started at {now_text()} with run_id={run_id}", flush=True)
    print(f"🧩 AGENT RUNNER VERSION: FINAL_FAST_TIMEOUT", flush=True)
    print(f"⏱ Per-row timeout: {ROW_TIMEOUT_SECONDS}s", flush=True)

    sheets.add_log(
        row_number="",
        status="AGENT_STARTED",
        log_type=agent_name,
        message=f"{agent_name} started with run_id={run_id}"
    )

    tasks = get_fast_agent_tasks(direction)

    if not tasks:
        print(f"✅ {agent_name}: no pending rows found.", flush=True)
        sheets.add_log(
            row_number="",
            status="NO_ROWS_LEFT",
            log_type=agent_name,
            message="No pending rows found"
        )
        sheets.flush_logs()
        return

    print(f"📌 {agent_name}: loaded {len(tasks)} pending rows for this run", flush=True)

    for row_num, url in tasks:
        if time.time() >= deadline:
            print(f"⏰ {agent_name}: max runtime reached", flush=True)
            break

        try:
            claim_row(row_num, agent_name, run_id)

            sheets.add_log(
                row_number=row_num,
                status="ROW_CLAIMED",
                log_type=agent_name,
                url=url,
                message=f"{agent_name} claimed row {row_num}"
            )

            row_start = time.time()
            print(f"🔍 {agent_name}: scraping row {row_num}", flush=True)

            ok, message = scrape_with_timeout(row_num, url)

            duration = round(time.time() - row_start, 1)

            if ok:
                sheets.mark_agent_done(row_num, agent_name)
                processed_count += 1
                print(f"✅ {agent_name}: finished row {row_num} in {duration}s", flush=True)
            else:
                if message == "TIMEOUT":
                    timeout_count += 1
                    sheets.mark_agent_timeout(row_num)
                    print(f"⏭ {agent_name}: row {row_num} timed out in {duration}s, moving next", flush=True)
                    sheets.add_log(
                        row_number=row_num,
                        status="ROW_TIMEOUT",
                        log_type=agent_name,
                        url=url,
                        message=f"Row timed out after {ROW_TIMEOUT_SECONDS}s"
                    )
                else:
                    error_count += 1
                    print(f"❌ {agent_name}: error row {row_num}: {message}", flush=True)
                    sheets.add_log(
                        row_number=row_num,
                        status="AGENT_ROW_ERROR",
                        log_type=agent_name,
                        url=url,
                        message=str(message)
                    )

        except Exception as e:
            error_count += 1
            print(f"❌ {agent_name}: outer error row {row_num}: {e}", flush=True)
            sheets.add_log(
                row_number=row_num,
                status="AGENT_ROW_ERROR",
                log_type=agent_name,
                url=url,
                message=str(e)
            )

        time.sleep(2)

    sheets.add_log(
        row_number="",
        status="AGENT_STOPPED",
        log_type=agent_name,
        message=(
            f"{agent_name} stopped. "
            f"Processed rows: {processed_count}, "
            f"Timeouts: {timeout_count}, "
            f"Errors: {error_count}"
        )
    )
    sheets.flush_logs()

    print(
        f"🛑 {agent_name} stopped at {now_text()}. "
        f"Processed: {processed_count}, Timeouts: {timeout_count}, Errors: {error_count}",
        flush=True
    )


if __name__ == "__main__":
    multiprocessing.set_start_method("spawn", force=True)

    if len(sys.argv) < 2:
        print("Usage: python agent_runner.py top", flush=True)
        print("Usage: python agent_runner.py bottom", flush=True)
        sys.exit(1)

    run_agent(sys.argv[1])
