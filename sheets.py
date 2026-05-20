import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

def save_to_sheet(data: dict):
    creds = Credentials.from_service_account_file("creds.json", scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open("Ad Scraper").sheet1

    sheet.append_row([
        data["advertiser"],
        data["name"],
        data["ad_url"],
        data["app_link"],
        data["video_id"]
    ])
    print("✅ Row saved to Google Sheets")