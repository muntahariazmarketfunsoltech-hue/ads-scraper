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

def get_urls_with_rows():
    """Fetches URLs and explicitly maps them to their exact Google Sheet row number."""
    sheet = get_sheet()
    # Column H = index 8
    all_values = sheet.col_values(8)  
    
    url_rows = []
    # all_values[0] is row 1 (header). all_values[1] is row 2.
    for index, value in enumerate(all_values):
        if index == 0:
            continue  # Skip header
            
        url = value.strip()
        if url:
            # Enumerate is 0-indexed, but Google Sheets rows are 1-indexed.
            row_number = index + 1 
            url_rows.append((row_number, url))
            
    return url_rows