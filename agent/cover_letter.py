"""
agent/cover_letter.py — Generate tailored cover letters using Claude.

Saves output to output/cover_letters/ and updates the Google Sheet.
"""

import json
import logging
import pathlib
import re
from datetime import date

import anthropic

import config
from agent.sheets import STATUS, update_status

logger = logging.getLogger(__name__)

OUTPUT_DIR = pathlib.Path(__file__).parent.parent / "output" / "cover_letters"


def _sanitise_filename(text: str) -> str:
    """Strip non-alphanumeric characters for safe filenames."""
    return re.sub(r"[^\w\-]", "_", text)[:40]


def write_cover_letter(cv: dict, job: dict, sheet=None) -> str | None:
    """
    Generate a cover letter for *job* tailored to *cv*.

    Args:
        cv:    Loaded cv.json dict.
        job:   Job dict with keys: title, company, url, snippet, score_reason (optional).
        sheet: Optional gspread worksheet for status updates.

    Returns:
        Absolute path to the saved cover letter file, or None on failure.
    """
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    company = job.get("company", "the company")
    role = job.get("title", "the role")
    snippet = job.get("snippet", "")
    score_reason = job.get("score_reason", "")

    system_prompt = (
        "You are an expert career coach writing a cover letter on behalf of the candidate. "
        "Follow the tone and style instructions precisely. "
        "Return ONLY the cover letter text — no subject line, no metadata, no markdown."
    )

    user_prompt = f"""## Candidate CV
{json.dumps(cv, indent=2)}

## Job Details
Company: {company}
Role: {role}
Job Description / Snippet:
{snippet}

## Why This Job Scores Well (context for you)
{score_reason}

## Tone & Style Instructions
{config.TONE}

## Cover Letter Requirements
- Do NOT open with "I am writing to apply" or any generic opener.
- Open with a strong commercial hook that immediately demonstrates value or relevant insight.
- Paragraph 1 (3-4 sentences): Hook + why this specific role at this specific company excites you.
- Paragraph 2 (4-5 sentences): Most relevant experience — be concrete, tie in cybersecurity academic background as a differentiator where relevant.
- Paragraph 3 (2-3 sentences): Confident call to action. No fluff.
- Maximum 3 paragraphs. No bullet points. No sign-off block — just the body paragraphs.
- Reference specific details from the job description above."""

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        letter_text = response.content[0].text.strip()
    except anthropic.APIError as exc:
        logger.error("Claude API error writing cover letter for %s: %s", job.get("url"), exc)
        return None
    except Exception as exc:
        logger.error("Unexpected error writing cover letter for %s: %s", job.get("url"), exc)
        return None

    # Save to file
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    filename = f"{_sanitise_filename(company)}_{_sanitise_filename(role)}_{today}.txt"
    filepath = OUTPUT_DIR / filename

    try:
        filepath.write_text(letter_text, encoding="utf-8")
        logger.info("Cover letter saved: %s", filepath)
    except OSError as exc:
        logger.error("Failed to save cover letter to %s: %s", filepath, exc)
        return None

    # Update sheet
    if sheet:
        update_status(
            sheet,
            job.get("url", ""),
            STATUS["cover"],
            extras={"cover_letter": str(filepath)},
        )

    return str(filepath)


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
        "company": "Acme Insurers",
        "url": "https://example.com/job/123",
        "snippet": (
            "Seeking a commercially driven sales executive to grow our cyber insurance "
            "book. OTE £90k. Hybrid London. B2B tech or insurance sales background preferred."
        ),
        "score_reason": "Strong alignment with cyber background and sales track record.",
    }

    path = write_cover_letter(cv, test_job)
    if path:
        print("Saved to:", path)
        print(open(path).read())
