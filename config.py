# config.py
# config.py

# 1. Your Google Sheet ID
# You can find this in the URL of your Google Sheet.
# Example: If your URL is https://docs.google.com/spreadsheets/d/1BxiMVs0X_5B329snIG2PNw/edit
# Your ID is: 1BxiMVs0X_5B329snIG2PNw
SPREADSHEET_ID = '1NDp5gwAYsdj-tC4mNqSDECJ9eXwD6fG4zFK6vFcy17E' 

# 2. Your Worksheet (Tab) Name
# This is the name on the tab at the bottom of your Google Sheet.
# It usually defaults to 'Sheet1', but change it if you renamed the tab.
WORKSHEET_NAME = 'Ad Scraper'                   

# 3. Your Credentials File Name
# This must exactly match the JSON file you saved in your ADS-SCRAPER folder.
CREDENTIALS_FILE = 'creds.json'
# --- Google Sheets ---
SHEET_NAME = "Ad Scraper"          # must match your Google Sheet name exactly
CREDS_FILE = "creds.json"          # your downloaded service account key

# --- Scraper settings ---
HEADLESS = True                   
WAIT_TIMEOUT = 5000                # how long to wait after clicking play (milliseconds)

# --- Ad URLs to scrape ---
# Add as many Google Ads Transparency Center URLs as you want here
