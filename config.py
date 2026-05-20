# config.py

# --- Google Sheets ---
SHEET_NAME = "Ad Scraper"          # must match your Google Sheet name exactly
CREDS_FILE = "creds.json"          # your downloaded service account key

# --- Scraper settings ---
HEADLESS = False                   # False = you can see the browser (good for debugging)
WAIT_TIMEOUT = 5000                # how long to wait after clicking play (milliseconds)

# --- Ad URLs to scrape ---
# Add as many Google Ads Transparency Center URLs as you want here
AD_URLS = [
    "https://adstransparency.google.com/advertiser/AR...",  # replace with real URL
    # "https://adstransparency.google.com/advertiser/AR...", # add more here
]