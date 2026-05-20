# sheets.py
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import config

def get_sheet():
    """Authenticates and returns the Google Sheet object."""
    scope = [
        "https://spreadsheets.google.com/feeds", 
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(config.CREDENTIALS_FILE, scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(config.SPREADSHEET_ID).worksheet(config.WORKSHEET_NAME)
    return sheet

def get_urls():
    """Fetches all URLs from Column H (8th column), skipping the header row."""
    sheet = get_sheet()
    # col_values(8) targets Column H
    records = sheet.col_values(8)
    if len(records) > 1:
        return records[1:] # Skip the 'transparency_url' header in row 1
    return []

def update_row(row_index, data):
    """
    Writes data back to Columns A through E.
    data format: [advertiser, name, ad_url, app_link, video_id]
    """
    sheet = get_sheet()
    # Update range is now A to E
    cell_range = f'A{row_index}:E{row_index}'
    sheet.update(values=[data], range_name=cell_range)