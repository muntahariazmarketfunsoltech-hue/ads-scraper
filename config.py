# config.py

# --- Google Sheets ---
SHEET_NAME = "Ad Scraper"          # must match your Google Sheet name exactly
CREDS_FILE = "creds.json"          # your downloaded service account key

# --- Scraper settings ---
HEADLESS = True                   
WAIT_TIMEOUT = 5000                # how long to wait after clicking play (milliseconds)

# --- Ad URLs to scrape ---
# Add as many Google Ads Transparency Center URLs as you want here
