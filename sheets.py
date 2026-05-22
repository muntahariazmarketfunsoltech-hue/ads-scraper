import time
import gspread
from google.oauth2.service_account import Credentials
import config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def get_sheet(retries=5, delay=3):
    """Get worksheet with retry on connection errors."""
    for attempt in range(1, retries + 1):
        try:
            creds = Credentials.from_service_account_file(
                config.CREDENTIALS_FILE, scopes=SCOPES
            )
            client = gspread.authorize(creds)
            sheet = client.open_by_key(config.SPREADSHEET_ID).worksheet(
                config.WORKSHEET_NAME
            )
            return sheet
        except Exception as e:
            print(f"⚠️  Sheets connect attempt {attempt}/{retries}: {e}")
            if attempt < retries:
                time.sleep(delay * attempt)
    raise ConnectionError("❌ Could not connect to Google Sheets after all retries.")


def get_urls():
    sheet = get_sheet()
    # Column H = index 8, data starts at row 2 so skip the first value (header)
    all_values = sheet.col_values(8)  # col_values is 1-indexed: A=1, H=8
    return [v.strip() for v in all_values[1:] if v.strip()]  # skip header, skip blanks


def update_row(row_num, data, retries=5, delay=3):
    """Update a row with retry on connection errors."""
    for attempt in range(1, retries + 1):
        try:
            sheet = get_sheet()
            sheet.update(f"A{row_num}:E{row_num}", [data])
            return
        except Exception as e:
            print(f"⚠️  Sheets update_row {row_num} attempt {attempt}/{retries}: {e}")
            if attempt < retries:
                time.sleep(delay * attempt)
    raise ConnectionError(f"❌ Could not update row {row_num} after all retries.")