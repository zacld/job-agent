"""
agent/apply.py — Playwright-based form filler, guided by Claude Vision.

Flow per job:
  1. Open the job URL in Chromium.
  2. Screenshot the page.
  3. Send screenshot + CV to Claude Vision — receive field-fill instructions.
  4. Fill each field using accessible selectors.
  5. Handle multi-page forms (up to 5 pages).
  6. Handle CV file upload.
  7. Take a final screenshot — save to output/screenshots/.
  8. Do NOT auto-submit.
  9. Update sheet status to 🟠 Queued.
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
from agent.sheets import STATUS, update_status

logger = logging.getLogger(__name__)

SCREENSHOT_DIR = pathlib.Path(__file__).parent.parent / "output" / "screenshots"
CV_PDF_PATH = pathlib.Path(__file__).parent.parent / "data" / "cv.pdf"  # optional

VISION_SYSTEM_PROMPT = """\
You are a job application form assistant. I will give you a screenshot of a job application
form and the candidate's CV as JSON. Return ONLY valid JSON — no markdown, no code fences.

Return a JSON object with a single key "fields" whose value is a list of objects:
[
  {
    "label": "<visible label text or best guess>",
    "value": "<value to enter, from CV>",
    "field_type": "<text|textarea|select|checkbox|file>",
    "selector_hint": "<placeholder text, aria-label, or visible label — whichever is most unique>"
  }
]

Rules:
- Only include fields that are visible and fillable on screen.
- For file upload fields set field_type to "file".
- For checkboxes that should be checked, set value to "true".
- If a field should be left empty (e.g. not applicable), omit it from the list.
- Prefer the most specific selector_hint that uniquely identifies the field.
- For "Submit" or "Next" buttons, do NOT include them in the fields list.
"""


def _screenshot_b64(page: Page) -> str:
    """Take a screenshot and return it base64-encoded."""
    png_bytes = page.screenshot(full_page=True)
    return base64.b64encode(png_bytes).decode("utf-8")


def _ask_claude_vision(client: anthropic.Anthropic, screenshot_b64: str, cv: dict) -> list[dict]:
    """Send screenshot + CV to Claude Vision and return list of field instructions."""
    user_content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": screenshot_b64,
            },
        },
        {
            "type": "text",
            "text": (
                f"Here is the candidate's CV as JSON:\n{json.dumps(cv, indent=2)}\n\n"
                "Please return the fields list as described in the system prompt."
            ),
        },
    ]

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=2048,
            system=VISION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text.strip()
        data = json.loads(raw)
        return data.get("fields", [])
    except json.JSONDecodeError as exc:
        logger.error("Claude Vision returned invalid JSON: %s", exc)
        return []
    except anthropic.APIError as exc:
        logger.error("Claude Vision API error: %s", exc)
        return []
    except Exception as exc:
        logger.error("Unexpected Vision error: %s", exc)
        return []


def _fill_field(page: Page, field: dict) -> None:
    """Attempt to fill a single form field using accessible selectors."""
    label = field.get("label", "")
    value = field.get("value", "")
    field_type = field.get("field_type", "text")
    hint = field.get("selector_hint", "")

    locator = None
    # Try multiple selector strategies in order of preference
    strategies = [
        lambda: page.get_by_label(label, exact=False) if label else None,
        lambda: page.get_by_placeholder(hint, exact=False) if hint else None,
        lambda: page.get_by_label(hint, exact=False) if hint else None,
        lambda: page.locator(f"[aria-label*='{hint}']") if hint else None,
        lambda: page.locator(f"[name*='{hint.lower().replace(' ', '_')}']") if hint else None,
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
        logger.warning("Could not find field %r — skipping.", label or hint)
        return

    try:
        if field_type == "file":
            cv_path = str(CV_PDF_PATH) if CV_PDF_PATH.exists() else ""
            if cv_path:
                locator.set_input_files(cv_path)
                logger.info("Uploaded file to field: %s", label)
            else:
                logger.warning("CV PDF not found at %s — skipping file upload.", CV_PDF_PATH)

        elif field_type == "checkbox":
            if value.lower() == "true":
                locator.check()
            else:
                locator.uncheck()
            logger.info("Set checkbox %r to %s", label, value)

        elif field_type == "select":
            locator.select_option(label=value)
            logger.info("Selected %r for %r", value, label)

        elif field_type == "textarea":
            locator.fill(value)
            logger.info("Filled textarea %r", label)

        else:  # text / email / tel / number
            locator.fill(value)
            logger.info("Filled field %r = %r", label, value)

    except PWTimeout:
        logger.warning("Timeout filling field %r", label)
    except Exception as exc:
        logger.warning("Error filling field %r: %s", label, exc)


def _save_screenshot(page: Page, company: str, role: str) -> str:
    """Save a final screenshot and return its path."""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    safe = lambda s: re.sub(r"[^\w\-]", "_", s)[:30]
    filename = f"{safe(company)}_{safe(role)}_{today}.png"
    filepath = SCREENSHOT_DIR / filename
    page.screenshot(path=str(filepath), full_page=True)
    logger.info("Screenshot saved: %s", filepath)
    return str(filepath)


def fill_application(cv: dict, job: dict, sheet=None) -> str | None:
    """
    Open the job URL, fill the form using Claude Vision guidance, and queue it.

    Args:
        cv:    Loaded cv.json dict.
        job:   Job dict with keys: url, company, title.
        sheet: Optional gspread worksheet for status updates.

    Returns:
        Screenshot path if successful, None on failure.
    """
    url = job.get("url", "")
    company = job.get("company", "Unknown")
    role = job.get("title", "Unknown")

    if not url:
        logger.error("fill_application: no URL provided.")
        return None

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    screenshot_path = None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=config.HEADLESS)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            page.goto(url, timeout=30_000)
            page.wait_for_load_state("networkidle", timeout=15_000)

            for page_num in range(1, 6):  # max 5 pages
                logger.info("Processing form page %d for %s", page_num, url)
                screenshot_b64 = _screenshot_b64(page)

                fields = _ask_claude_vision(client, screenshot_b64, cv)
                if not fields:
                    logger.info("No fillable fields found on page %d — stopping.", page_num)
                    break

                for field in fields:
                    _fill_field(page, field)

                # Check for a "Next" button to advance multi-page forms
                next_btn = None
                for next_label in ["Next", "Continue", "Next Step", "Proceed"]:
                    try:
                        btn = page.get_by_role("button", name=next_label, exact=False)
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
                        logger.warning("Could not click Next on page %d: %s", page_num, exc)
                        break
                else:
                    logger.info("No Next button found — form fill complete.")
                    break

            # Final screenshot — do NOT submit
            screenshot_path = _save_screenshot(page, company, role)
            browser.close()

    except Exception as exc:
        logger.error("fill_application failed for %s: %s", url, exc)
        return None

    # Update sheet
    if sheet and url:
        update_status(
            sheet, url, STATUS["queued"],
            extras={"notes": f"Screenshot: {screenshot_path}"},
        )

    return screenshot_path


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import pathlib as _pl

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
