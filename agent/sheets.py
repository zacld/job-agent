"""
agent/sheets.py — Google Sheets read/write helpers via gspread.

Authentication uses a service account whose JSON credentials are stored
base64-encoded in the GOOGLE_CREDENTIALS_JSON environment variable.
"""

import base64
import json
import logging
import tempfile
from datetime import date

import gspread
from google.oauth2.service_account import Credentials

import config

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column indices (1-based as gspread uses)
COL = {
    "date_found":     1,   # A
    "company":        2,   # B
    "role":           3,   # C
    "url":            4,   # D
    "score":          5,   # E
    "score_reason":   6,   # F
    "apply_method":   7,   # G
    "status":         8,   # H
    "date_applied":   9,   # I
    "cover_letter":  10,   # J
    "contact_email": 11,   # K
    "response":      12,   # L
    "notes":         13,   # M
}

HEADERS = [
    "Date Found", "Company", "Role Title", "Source URL",
    "Score", "Score Reason", "Apply Method", "Status",
    "Date Applied", "Cover Letter Path", "Contact Email", "Response", "Notes",
]

STATUS = {
    "found":       "⚪ Found",
    "scored":      "🔵 Scored",
    "cover":       "🟡 Cover Letter Written",
    "queued":      "🟠 Queued",
    "applied":     "🟢 Applied",
    "response":    "📬 Response Received",
    "rejected":    "❌ Rejected",
    "interview":   "⭐ Interview",
    "skipped":     "⏭ Skipped",
}


def get_sheet() -> gspread.Worksheet:
    """Authenticate and return the target worksheet, creating headers if new."""
    creds_b64 = config.GOOGLE_CREDENTIALS_JSON
    if not creds_b64:
        raise EnvironmentError("GOOGLE_CREDENTIALS_JSON env var is not set.")

    try:
        creds_json = base64.b64decode(creds_b64).decode("utf-8")
        creds_dict = json.loads(creds_json)
    except Exception as exc:
        raise ValueError(f"Failed to decode GOOGLE_CREDENTIALS_JSON: {exc}") from exc

    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)

    try:
        spreadsheet = client.open(config.SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        logger.info("Sheet %r not found — creating it.", config.SHEET_NAME)
        spreadsheet = client.create(config.SHEET_NAME)

    worksheet = spreadsheet.sheet1

    # Ensure headers exist
    existing = worksheet.row_values(1)
    if existing != HEADERS:
        logger.info("Writing sheet headers.")
        worksheet.insert_row(HEADERS, index=1)

    return worksheet


def job_exists(sheet: gspread.Worksheet, url: str) -> bool:
    """Return True if *url* is already in column D."""
    try:
        urls = sheet.col_values(COL["url"])
        return url in urls
    except Exception as exc:
        logger.error("job_exists check failed: %s", exc)
        return False


def add_job(sheet: gspread.Worksheet, job_data: dict) -> None:
    """Append a new row for *job_data* with status ⚪ Found."""
    row = [""] * len(HEADERS)
    row[COL["date_found"] - 1]  = job_data.get("date_found", date.today().isoformat())
    row[COL["company"] - 1]     = job_data.get("company", "")
    row[COL["role"] - 1]        = job_data.get("title", "")
    row[COL["url"] - 1]         = job_data.get("url", "")
    row[COL["status"] - 1]      = STATUS["found"]
    try:
        sheet.append_row(row, value_input_option="USER_ENTERED")
        logger.info("Added job to sheet: %s", job_data.get("url"))
    except Exception as exc:
        logger.error("Failed to add job to sheet: %s", exc)


def update_status(
    sheet: gspread.Worksheet,
    url: str,
    status: str,
    extras: dict | None = None,
) -> None:
    """
    Find the row whose column D matches *url*, update its status, and apply
    any extra field values supplied in *extras* (keyed by COL key names).
    """
    extras = extras or {}
    try:
        cell = sheet.find(url, in_column=COL["url"])
        if cell is None:
            logger.warning("update_status: URL not found in sheet: %s", url)
            return
        row_num = cell.row
        sheet.update_cell(row_num, COL["status"], status)
        for field, value in extras.items():
            col_idx = COL.get(field)
            if col_idx:
                sheet.update_cell(row_num, col_idx, value)
        logger.info("Updated status for %s → %s", url, status)
    except Exception as exc:
        logger.error("update_status failed for %s: %s", url, exc)


def get_jobs_by_status(sheet: gspread.Worksheet, status: str) -> list[dict]:
    """Return all rows whose status column matches *status*."""
    try:
        all_rows = sheet.get_all_records()
        return [r for r in all_rows if r.get("Status") == status]
    except Exception as exc:
        logger.error("get_jobs_by_status failed: %s", exc)
        return []


def get_todays_jobs(sheet: gspread.Worksheet) -> list[dict]:
    """Return all rows added today (column A == today's date)."""
    today = date.today().isoformat()
    try:
        all_rows = sheet.get_all_records()
        return [r for r in all_rows if str(r.get("Date Found", "")).startswith(today)]
    except Exception as exc:
        logger.error("get_todays_jobs failed: %s", exc)
        return []


def get_existing_urls(sheet: gspread.Worksheet) -> set[str]:
    """Return all URLs currently tracked in the sheet (column D)."""
    try:
        values = sheet.col_values(COL["url"])
        return set(v for v in values if v and v != "Source URL")
    except Exception as exc:
        logger.error("get_existing_urls failed: %s", exc)
        return set()


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sheet = get_sheet()
    print("Sheet ready:", sheet.title)
    print("Existing URLs:", get_existing_urls(sheet))
