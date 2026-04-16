"""
agent/apply.py — Playwright-based form filler, guided by Claude Vision.

Enhancements over v1:
  - Multi-turn confirmation loop: after each fill, re-screenshot and ask
    Claude whether fills succeeded and what errors are visible. Retries
    failed fields up to 3 times.
  - CAPTCHA detection: if a CAPTCHA is detected, skips and flags in sheet.
  - Login-wall detection: flags and skips rather than silently failing.
  - Validation error detection: re-sends screenshot with "what errors remain?"
    and attempts corrections.
  - All field operations still wrapped in try/except — one bad field never
    crashes the run.
  - headless=False locally, headless=True in CI (config.HEADLESS).
"""

import base64
import json
import logging
import pathlib
import re
import time
from datetime import date

import anthropic
from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeout

import config
from agent.retry import retry_anthropic
from agent.sheets import STATUS, update_status

logger = logging.getLogger(__name__)

SCREENSHOT_DIR = pathlib.Path(__file__).parent.parent / "output" / "screenshots"
CV_PDF_PATH    = pathlib.Path(__file__).parent.parent / "data" / "cv.pdf"

MAX_PAGES       = 5
MAX_FIELD_RETRY = 3


# ---------------------------------------------------------------------------
# Vision prompts
# ---------------------------------------------------------------------------

FILL_SYSTEM = """\
You are a job application form assistant. I will give you a screenshot of a job
application form and the candidate's CV as JSON.

Return ONLY valid JSON — no markdown, no code fences.

Return:
{
  "page_type": "<form|captcha|login|thankyou|error|unknown>",
  "fields": [
    {
      "label": "<visible label text>",
      "value": "<value to enter, from CV>",
      "field_type": "<text|textarea|select|checkbox|file>",
      "selector_hint": "<placeholder, aria-label, or unique visible label>"
    }
  ]
}

Rules:
- Set page_type to "captcha" if you see a CAPTCHA challenge.
- Set page_type to "login" if the page requires authentication before applying.
- Set page_type to "thankyou" if the application has already been submitted.
- Set page_type to "form" for normal fillable application pages.
- Only include fields visible and fillable right now.
- For file upload fields: field_type = "file".
- For checkboxes to check: value = "true".
- Omit fields that should be left blank.
- Do NOT include Submit or Next buttons in fields.
"""

CONFIRM_SYSTEM = """\
You are reviewing a job application form after fields have been filled.
Look at the screenshot and return ONLY valid JSON:

{
  "errors": ["<description of any visible validation error>"],
  "unfilled_required": ["<label of any required field still empty>"],
  "looks_complete": <true|false>,
  "has_next_button": <true|false>,
  "has_submit_button": <true|false>
}
"""

CORRECTION_SYSTEM = """\
A job application form field has a validation error. Return ONLY valid JSON:

{
  "corrected_value": "<corrected value to enter>",
  "selector_hint": "<best selector hint for this field>"
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _screenshot_b64(page: Page) -> str:
    return base64.b64encode(page.screenshot(full_page=True)).decode("utf-8")


def _image_message(b64: str) -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": b64},
    }


@retry_anthropic
def _claude_vision(client: anthropic.Anthropic, system: str, content: list) -> dict:
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": content}],
    )
    raw = resp.content[0].text.strip()
    return json.loads(raw)


def _ask_for_fields(client: anthropic.Anthropic, screenshot_b64: str, cv: dict) -> dict:
    """Return page_type + fields list."""
    content = [
        _image_message(screenshot_b64),
        {"type": "text", "text": f"Candidate CV:\n{json.dumps(cv, indent=2)}"},
    ]
    try:
        return _claude_vision(client, FILL_SYSTEM, content)
    except (json.JSONDecodeError, Exception) as exc:
        logger.error("Field detection failed: %s", exc)
        return {"page_type": "unknown", "fields": []}


def _ask_for_errors(client: anthropic.Anthropic, screenshot_b64: str) -> dict:
    """Return validation errors visible after a fill attempt."""
    content = [
        _image_message(screenshot_b64),
        {"type": "text", "text": "Please analyse this form screenshot for errors."},
    ]
    try:
        return _claude_vision(client, CONFIRM_SYSTEM, content)
    except (json.JSONDecodeError, Exception) as exc:
        logger.error("Error confirmation failed: %s", exc)
        return {"errors": [], "unfilled_required": [], "looks_complete": False,
                "has_next_button": False, "has_submit_button": False}


def _ask_for_correction(
    client: anthropic.Anthropic,
    screenshot_b64: str,
    field_label: str,
    error_desc: str,
    cv: dict,
) -> dict:
    content = [
        _image_message(screenshot_b64),
        {"type": "text", "text": (
            f"The field '{field_label}' has this error: {error_desc}\n"
            f"CV: {json.dumps(cv, indent=2)}\n"
            f"Suggest a corrected value and the best selector."
        )},
    ]
    try:
        return _claude_vision(client, CORRECTION_SYSTEM, content)
    except (json.JSONDecodeError, Exception) as exc:
        logger.error("Correction generation failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Field filling
# ---------------------------------------------------------------------------

def _fill_field(page: Page, field: dict) -> bool:
    """
    Attempt to fill one form field. Returns True on success.
    Tries multiple selector strategies in priority order.
    """
    label       = field.get("label", "")
    value       = field.get("value", "")
    field_type  = field.get("field_type", "text")
    hint        = field.get("selector_hint", "")

    locator = None
    strategies = [
        lambda: page.get_by_label(label, exact=False)       if label else None,
        lambda: page.get_by_placeholder(hint, exact=False)  if hint else None,
        lambda: page.get_by_label(hint, exact=False)        if hint else None,
        lambda: page.locator(f"[aria-label*='{hint}']")     if hint else None,
        lambda: page.locator(f"[name*='{hint.lower().replace(' ', '_')}']") if hint else None,
        lambda: page.get_by_role("textbox", name=label)     if label else None,
    ]

    for strategy in strategies:
        try:
            candidate = strategy()
            if candidate and candidate.count() > 0:
                locator = candidate.first
                break
        except Exception:
            continue

    if locator is None:
        logger.warning("Cannot find field %r — skipping.", label or hint)
        return False

    try:
        if field_type == "file":
            if CV_PDF_PATH.exists():
                locator.set_input_files(str(CV_PDF_PATH))
                logger.info("Uploaded CV to: %s", label)
            else:
                logger.warning("CV PDF missing — skipping file upload for %s.", label)
                return False

        elif field_type == "checkbox":
            locator.check() if value.lower() == "true" else locator.uncheck()
            logger.info("Checkbox %r = %s", label, value)

        elif field_type == "select":
            locator.select_option(label=value)
            logger.info("Select %r = %r", label, value)

        elif field_type == "textarea":
            locator.fill(value)
            logger.info("Textarea %r filled", label)

        else:
            locator.fill(value)
            logger.info("Field %r = %r", label, value[:60])

        return True

    except PWTimeout:
        logger.warning("Timeout on field %r", label)
    except Exception as exc:
        logger.warning("Error filling %r: %s", label, exc)
    return False


# ---------------------------------------------------------------------------
# Screenshot saving
# ---------------------------------------------------------------------------

def _save_screenshot(page: Page, company: str, role: str) -> str:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    today  = date.today().isoformat()
    safe   = lambda s: re.sub(r"[^\w\-]", "_", s)[:30]
    path   = SCREENSHOT_DIR / f"{safe(company)}_{safe(role)}_{today}.png"
    page.screenshot(path=str(path), full_page=True)
    logger.info("Screenshot saved: %s", path)
    return str(path)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fill_application(cv: dict, job: dict, sheet=None) -> str | None:
    """
    Open job URL, fill the form with Claude Vision guidance (multi-turn
    confirmation loop), save final screenshot. Does NOT auto-submit.

    Args:
        cv:    cv.json dict.
        job:   Job dict with url, company, title.
        sheet: Optional gspread worksheet.

    Returns:
        Screenshot path on success, None on failure.
    """
    url     = job.get("url", "")
    company = job.get("company", "Unknown")
    role    = job.get("title",   "Unknown")

    if not url:
        logger.error("fill_application: no URL.")
        return None

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    screenshot_path = None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=config.HEADLESS)
            ctx     = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = ctx.new_page()
            page.goto(url, timeout=30_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            for page_num in range(1, MAX_PAGES + 1):
                logger.info("Form page %d — %s", page_num, url)
                ss_b64 = _screenshot_b64(page)

                # ── Detect page type ──────────────────────────────────────
                vision_result = _ask_for_fields(client, ss_b64, cv)
                page_type     = vision_result.get("page_type", "unknown")
                fields        = vision_result.get("fields", [])

                if page_type == "captcha":
                    logger.warning("CAPTCHA detected on %s — flagging and skipping.", url)
                    if sheet:
                        update_status(sheet, url, STATUS["found"],
                                      extras={"notes": "CAPTCHA detected — manual apply needed"})
                    browser.close()
                    return None

                if page_type == "login":
                    logger.warning("Login wall on %s — flagging and skipping.", url)
                    if sheet:
                        update_status(sheet, url, STATUS["found"],
                                      extras={"notes": "Login required — manual apply needed"})
                    browser.close()
                    return None

                if page_type == "thankyou":
                    logger.info("Already submitted or thank-you page — stopping.")
                    break

                if not fields:
                    logger.info("No fields on page %d — stopping.", page_num)
                    break

                # ── Fill fields ───────────────────────────────────────────
                for field in fields:
                    _fill_field(page, field)

                time.sleep(0.8)

                # ── Confirmation loop ─────────────────────────────────────
                for retry_num in range(MAX_FIELD_RETRY):
                    ss_b64_after = _screenshot_b64(page)
                    confirm      = _ask_for_errors(client, ss_b64_after)
                    errors       = confirm.get("errors", [])
                    unfilled     = confirm.get("unfilled_required", [])

                    if not errors and not unfilled:
                        logger.info("Page %d looks clean after fill.", page_num)
                        break

                    logger.warning(
                        "Page %d — errors: %s | unfilled: %s (retry %d/%d)",
                        page_num, errors, unfilled, retry_num + 1, MAX_FIELD_RETRY,
                    )

                    # Attempt corrections for each error
                    for err in errors[:3]:  # cap at 3 correction attempts per pass
                        correction = _ask_for_correction(
                            client, ss_b64_after,
                            field_label=err,
                            error_desc=err,
                            cv=cv,
                        )
                        if correction.get("corrected_value") and correction.get("selector_hint"):
                            _fill_field(page, {
                                "label":         err,
                                "value":         correction["corrected_value"],
                                "field_type":    "text",
                                "selector_hint": correction["selector_hint"],
                            })

                    time.sleep(0.5)

                # ── Advance to next page ──────────────────────────────────
                if confirm.get("has_next_button"):
                    next_btn = None
                    for name in ["Next", "Continue", "Next Step", "Proceed"]:
                        try:
                            btn = page.get_by_role("button", name=name, exact=False)
                            if btn.count() > 0:
                                next_btn = btn.first
                                break
                        except Exception:
                            pass
                    if next_btn:
                        try:
                            next_btn.click()
                            page.wait_for_load_state("networkidle", timeout=10_000)
                            time.sleep(1)
                        except Exception as exc:
                            logger.warning("Could not click Next: %s", exc)
                            break
                else:
                    break  # No next button — we're on the final page

            # Final screenshot — do NOT submit
            screenshot_path = _save_screenshot(page, company, role)
            browser.close()

    except Exception as exc:
        logger.error("fill_application failed for %s: %s", url, exc)
        return None

    if sheet and url:
        update_status(sheet, url, STATUS["queued"],
                      extras={"notes": f"Screenshot: {screenshot_path}"})

    return screenshot_path


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json, pathlib as _pl

    logging.basicConfig(level=logging.INFO)

    cv_path = _pl.Path(__file__).parent.parent / "data" / "cv.json"
    with open(cv_path) as f:
        cv = json.load(f)

    test_job = {
        "title": "Cyber Insurance Sales Executive",
        "company": "Demo Corp",
        "url": "https://example.com/apply",
    }

    result = fill_application(cv, test_job)
    print("Screenshot:", result)
