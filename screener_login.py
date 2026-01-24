#!/usr/bin/env python3
"""Screener.in concalls scraper - with PDF extraction and Google integrations."""

from __future__ import annotations

import os
import sys
import time
import re
import csv
import tempfile
import base64
import json
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from functools import wraps

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException
import pdfplumber
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# =============================================================================
# CONFIGURATION
# =============================================================================

# Scraper settings
TARGET_CONCALL_COUNT = 100
PAGE_LOAD_TIMEOUT = 10  # seconds
REQUEST_TIMEOUT = 30  # seconds
RATE_LIMIT_DELAY = 0.3  # seconds between PDF downloads

# Google Sheets settings
SHEET_NAME = "Screener Concalls"
CREDENTIALS_FILE = "credentials.json"

# Google Calendar settings
CALENDAR_ID = "e9b665f1aa7c91203430bcad9af20c3df9d9f4aa45ffe455cb2be475396b1d07@group.calendar.google.com"
CONCALL_DURATION_HOURS = 1

# Calendar color IDs (1-11): Lavender, Sage, Grape, Flamingo, Banana, Tangerine, Peacock, Graphite, Blueberry, Basil, Tomato
CALENDAR_COLORS = ['1', '2', '3', '4', '5', '6', '7', '9', '10', '11']  # Skip 8 (Graphite)

# =============================================================================
# LOGGING SETUP
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Suppress noisy loggers
logging.getLogger("pdfminer").setLevel(logging.ERROR)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("selenium").setLevel(logging.WARNING)


# =============================================================================
# UTILITY FUNCTIONS
# =============================================================================

def get_requests_session() -> requests.Session:
    """Create a requests session with retry logic."""
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def now_ist() -> datetime:
    """Get current time in IST (UTC+5:30)."""
    ist = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(ist)


def now_utc() -> datetime:
    """Get current time in UTC (timezone-aware)."""
    return datetime.now(timezone.utc)


def get_google_credentials() -> Credentials:
    """Get Google credentials from file or environment variable.

    Returns:
        Google service account credentials with required scopes.

    Raises:
        FileNotFoundError: If no credentials are found.
        ValueError: If credentials are invalid.
    """
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/calendar"
    ]

    # Try environment variable first (for GitHub Actions)
    creds_b64 = os.environ.get("GOOGLE_CREDENTIALS_BASE64")
    if creds_b64:
        try:
            creds_json = base64.b64decode(creds_b64).decode('utf-8')
            creds_dict = json.loads(creds_json)
            logger.debug("Using credentials from environment variable")
            return Credentials.from_service_account_info(creds_dict, scopes=scopes)
        except (json.JSONDecodeError, ValueError) as e:
            raise ValueError(f"Invalid GOOGLE_CREDENTIALS_BASE64: {e}") from e

    # Fall back to local file
    if os.path.exists(CREDENTIALS_FILE):
        logger.debug(f"Using credentials from {CREDENTIALS_FILE}")
        return Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)

    raise FileNotFoundError(
        "No Google credentials found. Set GOOGLE_CREDENTIALS_BASE64 env var "
        f"or provide {CREDENTIALS_FILE}"
    )


def write_to_google_sheets(concalls: list[dict]) -> str:
    """Write concalls data to Google Sheets.

    Args:
        concalls: List of concall dictionaries with keys: company, date, time, phone, pdf_url

    Returns:
        URL of the Google Sheet.
    """
    logger.info("Connecting to Google Sheets...")

    creds = get_google_credentials()
    client = gspread.authorize(creds)

    # Try to open existing sheet or create new one
    try:
        sheet = client.open(SHEET_NAME)
        logger.info(f"Opened existing sheet: {SHEET_NAME}")
    except gspread.SpreadsheetNotFound:
        sheet = client.create(SHEET_NAME)
        logger.info(f"Created new sheet: {SHEET_NAME}")

    worksheet = sheet.sheet1
    worksheet.clear()

    # Prepare data
    headers = ["Company Name", "Date", "Time", "Phone Number", "PDF Link"]
    rows = [headers]
    for c in concalls:
        rows.append([c['company'], c['date'], c['time'], c['phone'], c['pdf_url']])

    # Write all data
    logger.info(f"Writing {len(concalls)} rows...")
    worksheet.update(rows, value_input_option='RAW')

    # Format header row (bold)
    worksheet.format('A1:E1', {
        'textFormat': {'bold': True},
        'backgroundColor': {'red': 0.9, 'green': 0.9, 'blue': 0.9}
    })

    # Set column widths
    column_widths = [150, 130, 110, 280, 450]
    sheet.batch_update({
        "requests": [
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": worksheet.id,
                        "dimension": "COLUMNS",
                        "startIndex": i,
                        "endIndex": i + 1
                    },
                    "properties": {"pixelSize": width},
                    "fields": "pixelSize"
                }
            }
            for i, width in enumerate(column_widths)
        ]
    })

    # Freeze header row
    worksheet.freeze(rows=1)

    logger.info(f"Sheet URL: {sheet.url}")
    return sheet.url


def parse_concall_datetime(date_str: str, time_str: str) -> Optional[datetime]:
    """Parse concall date and time strings into a datetime object.

    Args:
        date_str: Date string like "24 January 2026"
        time_str: Time string like "9:30:00 AM"

    Returns:
        Parsed datetime or None if parsing fails.
    """
    try:
        combined = f"{date_str} {time_str}"
        return datetime.strptime(combined, "%d %B %Y %I:%M:%S %p")
    except ValueError as e:
        logger.debug(f"Failed to parse datetime '{combined}': {e}")
        return None


def sync_to_google_calendar(concalls: list[dict]) -> tuple[int, int, int]:
    """Sync concalls to Google Calendar with smart duplicate handling and color coding.

    Args:
        concalls: List of concall dictionaries.

    Returns:
        Tuple of (created, updated, skipped) counts.
    """
    logger.info("Syncing to Google Calendar...")

    creds = get_google_credentials()
    service = build('calendar', 'v3', credentials=creds)

    # Pre-process: group concalls by their start time to assign colors
    time_slots: dict[str, list[str]] = {}
    current_time = datetime.now()

    for c in concalls:
        start_dt = parse_concall_datetime(c['date'], c['time'])
        if start_dt and start_dt >= current_time:
            time_key = start_dt.strftime('%Y-%m-%d %H:%M')
            if time_key not in time_slots:
                time_slots[time_key] = []
            time_slots[time_key].append(c['company'])

    # Create color mapping for overlapping events
    color_map: dict[str, str] = {}
    for time_key, companies in time_slots.items():
        if len(companies) > 1:
            for idx, company in enumerate(companies):
                color_map[f"{company}_{time_key}"] = CALENDAR_COLORS[idx % len(CALENDAR_COLORS)]

    # Get existing events (future events only)
    now_iso = now_utc().isoformat()
    existing_events: dict[str, dict] = {}

    try:
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=now_iso,
            maxResults=500,
            singleEvents=True
        ).execute()

        for event in events_result.get('items', []):
            props = event.get('extendedProperties', {}).get('private', {})
            if 'concall_id' in props:
                existing_events[props['concall_id']] = event
    except HttpError as e:
        logger.warning(f"Could not fetch existing events: {e}")

    created = 0
    updated = 0
    skipped = 0

    for c in concalls:
        start_dt = parse_concall_datetime(c['date'], c['time'])

        if not start_dt:
            logger.warning(f"Skipping {c['company']}: could not parse date/time")
            skipped += 1
            continue

        # Skip past events
        if start_dt < current_time:
            skipped += 1
            continue

        try:
            # Create unique ID (hash of company + date + time)
            concall_id = hashlib.md5(
                f"{c['company']}_{c['date']}_{c['time']}".encode()
            ).hexdigest()

            # Check for color assignment
            time_key = start_dt.strftime('%Y-%m-%d %H:%M')
            color_key = f"{c['company']}_{time_key}"
            color_id = color_map.get(color_key)

            # Calculate end time (handles midnight rollover correctly)
            end_dt = start_dt + timedelta(hours=CONCALL_DURATION_HOURS)

            # Build event description
            description = f"""📞 Dial-in: {c['phone']}

📅 Date: {c['date']}
⏰ Time: {c['time']}

📄 PDF Announcement:
{c['pdf_url']}

---
Auto-synced from Screener.in"""

            # Event body
            event_body = {
                'summary': f"📞 {c['company']} - Concall",
                'description': description,
                'start': {
                    'dateTime': start_dt.strftime('%Y-%m-%dT%H:%M:%S'),
                    'timeZone': 'Asia/Kolkata',
                },
                'end': {
                    'dateTime': end_dt.strftime('%Y-%m-%dT%H:%M:%S'),
                    'timeZone': 'Asia/Kolkata',
                },
                'extendedProperties': {
                    'private': {
                        'concall_id': concall_id
                    }
                },
                'reminders': {
                    'useDefault': False,
                    'overrides': [
                        {'method': 'popup', 'minutes': 15},
                        {'method': 'popup', 'minutes': 60},
                    ],
                },
            }

            # Add color if there are overlapping events
            if color_id:
                event_body['colorId'] = color_id

            if concall_id in existing_events:
                existing = existing_events[concall_id]
                if (existing.get('summary') != event_body['summary'] or
                    existing.get('description') != event_body['description'] or
                    existing.get('colorId') != event_body.get('colorId')):
                    service.events().update(
                        calendarId=CALENDAR_ID,
                        eventId=existing['id'],
                        body=event_body
                    ).execute()
                    updated += 1
                else:
                    skipped += 1
            else:
                service.events().insert(
                    calendarId=CALENDAR_ID,
                    body=event_body
                ).execute()
                created += 1

        except HttpError as e:
            logger.error(f"Calendar API error for {c['company']}: {e}")
            continue
        except Exception as e:
            logger.error(f"Unexpected error for {c['company']}: {e}")
            continue

    logger.info(f"Calendar sync complete - Created: {created}, Updated: {updated}, Skipped: {skipped}")
    return created, updated, skipped


def extract_phone_from_pdf(pdf_url: str, session: Optional[requests.Session] = None) -> str:
    """Download PDF and extract phone numbers.

    Args:
        pdf_url: URL of the PDF to download.
        session: Optional requests session with retry logic.

    Returns:
        Extracted phone number(s) or error message.
    """
    if session is None:
        session = get_requests_session()

    tmp_path = None

    try:
        # Download PDF with retry logic
        headers = {"User-Agent": "Mozilla/5.0 (compatible; ConcallsBot/1.0)"}
        response = session.get(pdf_url, headers=headers, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()

        # Save to temp file
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(response.content)
            tmp_path = tmp.name

        # Extract text from PDF
        text = ""
        with pdfplumber.open(tmp_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"

        # Find phone numbers using multiple patterns
        phone_patterns = [
            r'\+91[-\s]?\d{2}[-\s]?\d{4}[-\s]?\d{4}',  # +91 22 6280 1234
            r'\+91[-\s]?\d{10}',                         # +91 9876543210
            r'91[-\s]?\d{2}[-\s]?\d{4}[-\s]?\d{4}',     # 91 22 6280 1234
            r'\d{4}[-\s]?\d{3}[-\s]?\d{4}',             # 1800 123 4567
            r'\d{2,4}[-\s]?\d{4}[-\s]?\d{4}',           # 22 6280 1234
        ]

        phones = []
        for pattern in phone_patterns:
            matches = re.findall(pattern, text)
            phones.extend(matches)

        # Remove duplicates preserving order
        unique_phones = list(dict.fromkeys(phones))
        if unique_phones:
            return "; ".join(unique_phones[:3])
        return "Not found"

    except requests.exceptions.RequestException as e:
        logger.debug(f"PDF download failed for {pdf_url}: {e}")
        return "Download failed"
    except Exception as e:
        logger.debug(f"PDF extraction error for {pdf_url}: {e}")
        return f"Error: {str(e)[:30]}"
    finally:
        # Clean up temp file
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def create_chrome_driver() -> webdriver.Chrome:
    """Create a configured Chrome WebDriver instance.

    Returns:
        Configured Chrome WebDriver.
    """
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-gpu")

    return webdriver.Chrome(options=options)


def login_to_screener(driver: webdriver.Chrome, username: str, password: str) -> bool:
    """Login to Screener.in.

    Args:
        driver: Chrome WebDriver instance.
        username: Screener username.
        password: Screener password.

    Returns:
        True if login successful, False otherwise.
    """
    logger.info("Logging in to Screener.in...")
    driver.get("https://www.screener.in/login/")

    try:
        # Wait for login form
        WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
            EC.presence_of_element_located((By.NAME, "username"))
        )

        driver.find_element(By.NAME, "username").send_keys(username)
        driver.find_element(By.NAME, "password").send_keys(password)

        login_btn = driver.find_element(By.CSS_SELECTOR, "button[type='submit']")
        driver.execute_script("arguments[0].click();", login_btn)

        # Wait for redirect
        time.sleep(3)

        if "login" in driver.current_url.lower():
            logger.error("Login failed - still on login page")
            return False

        logger.info("Login successful")
        return True

    except TimeoutException:
        logger.error("Login page did not load in time")
        return False
    except NoSuchElementException as e:
        logger.error(f"Login form element not found: {e}")
        return False


def scrape_concalls_page(driver: webdriver.Chrome, page: int) -> list[dict]:
    """Scrape a single page of concalls.

    Args:
        driver: Chrome WebDriver instance.
        page: Page number to scrape.

    Returns:
        List of concall dictionaries from this page.
    """
    url = f"https://www.screener.in/concalls/upcoming/?p={page}"
    driver.get(url)

    try:
        WebDriverWait(driver, PAGE_LOAD_TIMEOUT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table"))
        )
    except TimeoutException:
        logger.warning(f"Page {page} did not load in time")
        return []

    concalls = []
    rows = driver.find_elements(By.CSS_SELECTOR, "table tr")

    for row in rows:
        try:
            th = row.find_element(By.TAG_NAME, "th")
            tds = row.find_elements(By.TAG_NAME, "td")

            if len(tds) >= 2:
                company = th.text.strip()
                date = tds[0].text.strip()
                time_str = tds[1].text.strip()

                # Get PDF link
                pdf_url = ""
                links = th.find_elements(By.TAG_NAME, "a")
                for link in links:
                    href = link.get_attribute("href") or ""
                    if ".pdf" in href.lower():
                        pdf_url = href
                        break

                if company and pdf_url:
                    concalls.append({
                        "company": company,
                        "date": date,
                        "time": time_str,
                        "pdf_url": pdf_url
                    })

        except NoSuchElementException:
            continue

    return concalls


def scrape_all_concalls(driver: webdriver.Chrome) -> list[dict]:
    """Scrape all concalls up to the target count.

    Args:
        driver: Chrome WebDriver instance.

    Returns:
        List of unique concall dictionaries.
    """
    logger.info(f"Fetching up to {TARGET_CONCALL_COUNT} concalls...")

    all_concalls = []
    page = 1

    while len(all_concalls) < TARGET_CONCALL_COUNT:
        page_concalls = scrape_concalls_page(driver, page)
        logger.info(f"Page {page}: found {len(page_concalls)} concalls")

        if not page_concalls:
            break

        all_concalls.extend(page_concalls)
        page += 1

    # Remove duplicates (same company + date + time)
    seen = set()
    unique_concalls = []
    for c in all_concalls:
        key = (c['company'], c['date'], c['time'])
        if key not in seen:
            seen.add(key)
            unique_concalls.append(c)

    result = unique_concalls[:TARGET_CONCALL_COUNT]
    logger.info(f"Total: {len(result)} unique concalls")
    return result


def extract_all_phone_numbers(concalls: list[dict]) -> None:
    """Extract phone numbers from all concall PDFs.

    Args:
        concalls: List of concall dictionaries (modified in place).
    """
    logger.info("Extracting phone numbers from PDFs...")
    session = get_requests_session()

    for i, c in enumerate(concalls):
        logger.info(f"[{i+1}/{len(concalls)}] {c['company'][:30]}")
        c['phone'] = extract_phone_from_pdf(c['pdf_url'], session)
        time.sleep(RATE_LIMIT_DELAY)


def sort_concalls_by_datetime(concalls: list[dict]) -> None:
    """Sort concalls by date and time (earliest first).

    Args:
        concalls: List of concall dictionaries (modified in place).
    """
    def get_sort_key(c: dict) -> datetime:
        dt = parse_concall_datetime(c['date'], c['time'])
        return dt if dt else datetime.max

    concalls.sort(key=get_sort_key)
    logger.info("Sorted concalls by date/time")


def save_to_csv(concalls: list[dict], filename: str = "concalls.csv") -> str:
    """Save concalls to CSV file.

    Args:
        concalls: List of concall dictionaries.
        filename: Output filename.

    Returns:
        Path to saved file.
    """
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Company Name", "Date", "Time", "Phone Number", "PDF Link"])
        for c in concalls:
            writer.writerow([c['company'], c['date'], c['time'], c['phone'], c['pdf_url']])

    logger.info(f"CSV backup saved: {filename}")
    return filename


def main() -> int:
    """Main entry point for the concalls scraper.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    # Validate credentials
    username = os.environ.get("SCREENER_USERNAME")
    password = os.environ.get("SCREENER_PASSWORD")

    if not username or not password:
        logger.error("Set SCREENER_USERNAME and SCREENER_PASSWORD environment variables")
        return 1

    driver = None

    try:
        # Initialize browser
        driver = create_chrome_driver()

        # Login
        if not login_to_screener(driver, username, password):
            return 1

        # Scrape concalls
        concalls = scrape_all_concalls(driver)

        if not concalls:
            logger.error("No concalls found")
            return 1

        # Extract phone numbers
        extract_all_phone_numbers(concalls)

        # Sort by date
        sort_concalls_by_datetime(concalls)

        # Save to CSV (backup)
        save_to_csv(concalls)

        # Write to Google Sheets
        sheet_url = write_to_google_sheets(concalls)

        # Sync to Google Calendar
        created, updated, skipped = sync_to_google_calendar(concalls)

        logger.info("=" * 60)
        logger.info(f"Done! {len(concalls)} concalls synced to Sheets & Calendar")
        logger.info("=" * 60)

        return 0

    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        return 1

    finally:
        if driver:
            driver.quit()


if __name__ == "__main__":
    sys.exit(main())
