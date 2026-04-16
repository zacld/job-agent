"""
agent/followup.py — Application follow-up scheduler.

Checks the Google Sheet for 🟢 Applied jobs where:
  - Date Applied is 7+ days ago
  - Status is still 🟢 Applied (no response received yet)

For each, Claude drafts a short, confident follow-up email. The draft is:
  - Stored in the sheet Notes column (so you can review before sending)
  - Flagged in the daily digest with a "Chase?" banner
  - Optionally sent automatically if AUTO_SEND_FOLLOWUPS=true in config

Follow-up tone: brief, not desperate. References the role and previous application.
One follow-up per job maximum (tracked in Notes column).
"""

import json
import logging
import smtplib
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import anthropic

import config
from agent.retry import retry_anthropic
from agent.sheets import STATUS, update_status, get_jobs_by_status

logger = logging.getLogger(__name__)

FOLLOWUP_AFTER_DAYS = 7
FOLLOWUP_MARKER     = "[FOLLOWUP_SENT]"


# ---------------------------------------------------------------------------
# Claude: draft follow-up email
# ---------------------------------------------------------------------------

@retry_anthropic
def _draft_followup(cv: dict, job: dict) -> dict:
    """
    Ask Claude to draft a follow-up email.
    Returns dict with 'subject' and 'body'.
    """
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    prompt = f"""Draft a brief follow-up email for a job application.

Candidate: {cv.get('full_name', 'Zachary Devine')}
Role applied for: {job.get('Role Title', '')}
Company: {job.get('Company', '')}
Date applied: {job.get('Date Applied', '')}
Cover letter excerpt / score reason: {job.get('Score Reason', '')}

Rules:
- Subject line: max 10 words, references the role, not "Following up on my application"
- Body: 3-4 sentences max
- Tone: confident and direct, not apologetic or desperate
- Remind them of one specific thing from the original application (use score reason)
- End with a clear single ask: a brief call or confirmation they received it
- No fluff, no "I hope this email finds you well"

Return ONLY valid JSON, no markdown:
{{
  "subject": "<subject line>",
  "body": "<email body>"
}}"""

    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = resp.content[0].text.strip()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def _send_followup_email(cv: dict, job: dict, draft: dict) -> bool:
    """Send the follow-up via Gmail SMTP."""
    to_addr   = job.get("Contact Email", "").strip()
    smtp_user = config.GMAIL_USER
    smtp_pass = config.GMAIL_APP_PASSWORD
    name      = cv.get("full_name", "Zachary Devine")
    from_addr = config.GMAIL_USER or cv.get("email", "")

    if not to_addr:
        logger.warning("No contact email for %s — cannot send follow-up.", job.get("Source URL"))
        return False

    if not smtp_user or not smtp_pass:
        logger.error("GMAIL credentials not set.")
        return False

    signature = (
        f"\n\n--\n{name}\n"
        f"{cv.get('phone', '')}\n"
        f"{cv.get('email', '')}\n"
        f"{cv.get('linkedin', '')}"
    )

    msg = MIMEMultipart("mixed")
    msg["Subject"] = draft["subject"]
    msg["From"]    = f"{name} <{from_addr}>"
    msg["To"]      = to_addr
    msg.attach(MIMEText(draft["body"] + signature, "plain", "utf-8"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [to_addr], msg.as_string())
        logger.info("Follow-up sent to %s for %s", to_addr, job.get("Role Title"))
        return True
    except Exception as exc:
        logger.error("Follow-up send failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_and_send_followups(
    cv: dict,
    sheet,
    auto_send: bool = False,
    dry_run: bool = False,
) -> list[dict]:
    """
    Find 🟢 Applied jobs overdue for a follow-up, draft emails, optionally send.

    Args:
        cv:        cv.json dict.
        sheet:     gspread worksheet.
        auto_send: If True, actually send the follow-up emails.
        dry_run:   If True, draft but do not send or update sheet.

    Returns:
        List of job dicts that need follow-up (for inclusion in digest).
    """
    applied_jobs = get_jobs_by_status(sheet, STATUS["applied"])
    today        = date.today()
    due_jobs     = []

    for job in applied_jobs:
        # Skip if already followed up
        notes = job.get("Notes", "")
        if FOLLOWUP_MARKER in notes:
            continue

        # Check date
        date_applied_str = job.get("Date Applied", "")
        if not date_applied_str:
            continue
        try:
            date_applied = date.fromisoformat(date_applied_str[:10])
        except ValueError:
            continue

        days_elapsed = (today - date_applied).days
        if days_elapsed < FOLLOWUP_AFTER_DAYS:
            continue

        logger.info(
            "Follow-up due (%d days): %s — %s",
            days_elapsed, job.get("Company"), job.get("Role Title"),
        )

        # Draft follow-up
        try:
            draft = _draft_followup(cv, job)
        except Exception as exc:
            logger.error("Draft failed for %s: %s", job.get("Source URL"), exc)
            draft = {
                "subject": f"Re: {job.get('Role Title', 'Application')} — following up",
                "body": "Hi, I wanted to follow up on my recent application. Please let me know if you need anything else.",
            }

        job["_followup_draft"] = draft
        due_jobs.append(job)

        if dry_run:
            logger.info(
                "DRY RUN — follow-up drafted.\n  Subject: %s\n  Body: %.200s",
                draft["subject"], draft["body"],
            )
            continue

        # Store draft in Notes
        new_notes = f"{notes}\n{FOLLOWUP_MARKER} Draft: {draft['subject']}".strip()
        url = job.get("Source URL", "")

        if auto_send:
            sent = _send_followup_email(cv, job, draft)
            if sent:
                update_status(sheet, url, STATUS["response"],
                              extras={"notes": new_notes + " [SENT]"})
        else:
            # Just save the draft to Notes for manual review
            if url and sheet:
                update_status(sheet, url, STATUS["applied"],
                              extras={"notes": new_notes})

    logger.info(
        "Follow-up check complete. %d jobs due for follow-up.", len(due_jobs)
    )
    return due_jobs


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json, pathlib

    logging.basicConfig(level=logging.INFO)

    cv_path = pathlib.Path(__file__).parent.parent / "data" / "cv.json"
    with open(cv_path) as f:
        cv = json.load(f)

    # Simulate a job that was applied to 8 days ago
    test_job = {
        "Role Title":    "Cyber Insurance Sales Executive",
        "Company":       "Acme Insurers",
        "Source URL":    "https://example.com/job/1",
        "Date Applied":  (date.today() - timedelta(days=8)).isoformat(),
        "Contact Email": "careers@acme-example.com",
        "Score Reason":  "Strong cyber background and B2B closing experience.",
        "Notes":         "",
    }

    try:
        draft = _draft_followup(cv, test_job)
        print("Subject:", draft["subject"])
        print()
        print(draft["body"])
    except Exception as e:
        print("Error:", e)
