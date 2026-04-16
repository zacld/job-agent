"""
agent/cover_letter.py — Generate tailored cover letters using Claude.

Enhancements:
  - Uses full job description text when available
  - Version tracking: v1, v2, v3 — never overwrites existing letters
  - Version history stored in sheet column J as JSON list of paths
  - Wrapped with retry_anthropic
"""

import json
import logging
import pathlib
import re
from datetime import date

import anthropic

import config
from agent.retry import retry_anthropic
from agent.sheets import STATUS, update_status

logger = logging.getLogger(__name__)

OUTPUT_DIR = pathlib.Path(__file__).parent.parent / "output" / "cover_letters"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitise(text: str) -> str:
    return re.sub(r"[^\w\-]", "_", text)[:40]


def _next_version_path(company: str, role: str) -> pathlib.Path:
    """
    Return the next versioned file path for this company+role.
    e.g. Acme_Cyber_Sales_2025-01-15_v1.txt, _v2.txt …
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    today  = date.today().isoformat()
    base   = f"{_sanitise(company)}_{_sanitise(role)}_{today}"

    for v in range(1, 100):
        path = OUTPUT_DIR / f"{base}_v{v}.txt"
        if not path.exists():
            return path
    # Fallback (should never happen)
    return OUTPUT_DIR / f"{base}_v99.txt"


def _load_existing_versions(sheet, url: str) -> list[str]:
    """Read existing cover letter paths from sheet column J (JSON list or plain string)."""
    if not sheet or not url:
        return []
    try:
        cell = sheet.find(url, in_column=4)  # col D = url
        if not cell:
            return []
        row = sheet.row_values(cell.row)
        cl_val = row[9] if len(row) > 9 else ""  # col J = index 9
        if not cl_val:
            return []
        try:
            versions = json.loads(cl_val)
            return versions if isinstance(versions, list) else [cl_val]
        except json.JSONDecodeError:
            return [cl_val]
    except Exception as exc:
        logger.warning("Could not read existing cover letter versions: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Claude call
# ---------------------------------------------------------------------------

@retry_anthropic
def _call_claude(client: anthropic.Anthropic, system: str, user: str) -> str:
    resp = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return resp.content[0].text.strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def write_cover_letter(
    cv: dict,
    job: dict,
    sheet=None,
    force_new_version: bool = False,
) -> str | None:
    """
    Generate a tailored cover letter for *job*.

    If a cover letter already exists for this job and force_new_version is
    False, returns the existing path without calling Claude again.

    Args:
        cv:                cv.json dict.
        job:               Job dict — needs title, company, url, description/snippet.
        sheet:             Optional gspread worksheet.
        force_new_version: Force a new Claude call even if one exists.

    Returns:
        Absolute path to the saved .txt file, or None on failure.
    """
    company      = job.get("company", "the_company")
    role         = job.get("title",   "the_role")
    url          = job.get("url",     "")
    description  = job.get("description") or job.get("snippet", "")
    score_reason = job.get("score_reason", "")

    # Check for existing version unless forced
    if not force_new_version:
        existing = _load_existing_versions(sheet, url)
        if existing:
            latest = existing[-1]
            if pathlib.Path(latest).exists():
                logger.info("Cover letter already exists (v%d) — skipping: %s", len(existing), latest)
                return latest
            logger.warning("Existing cover letter path not found on disk, regenerating.")

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

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
Job Description:
{description}

## Why This Job Scores Well (context only — do not quote this)
{score_reason}

## Tone & Style
{config.TONE}

## Cover Letter Requirements
- Do NOT open with "I am writing to apply" or any variation.
- Open with a strong commercial hook that immediately signals value or insight.
- Paragraph 1 (3-4 sentences): Hook + why this specific role at this specific company.
- Paragraph 2 (4-5 sentences): Most relevant experience — be concrete and quantified.
  Where relevant, use the cybersecurity/AI academic background as a commercial differentiator.
- Paragraph 3 (2-3 sentences): Confident, specific call to action. No filler.
- Maximum 3 paragraphs. No bullet points. No sign-off block (body only).
- Reference specific details from the job description — show you read it.
- Do not use words: synergy, passionate, leverage (as a verb), stakeholder journey."""

    try:
        letter_text = _call_claude(client, system_prompt, user_prompt)
    except Exception as exc:
        logger.error("Cover letter generation failed for %s: %s", url, exc)
        return None

    # Save with version number
    filepath = _next_version_path(company, role)
    try:
        filepath.write_text(letter_text, encoding="utf-8")
        logger.info("Cover letter saved: %s", filepath)
    except OSError as exc:
        logger.error("Failed to save cover letter: %s", exc)
        return None

    # Update sheet — store version history as JSON list
    if sheet and url:
        existing = _load_existing_versions(sheet, url)
        existing.append(str(filepath))
        versions_json = json.dumps(existing)
        update_status(
            sheet, url, STATUS["cover"],
            extras={"cover_letter": versions_json},
        )

    return str(filepath)


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
        "company": "Acme Insurers",
        "url": "https://example.com/job/123",
        "description": (
            "Seeking a commercially driven sales professional to own and grow our "
            "cyber insurance book. You will manage the full sales cycle, from pipeline "
            "generation through to close, working with CISOs and risk managers. "
            "Base £55,000–£65,000, OTE £90,000. Hybrid London. "
            "B2B tech or insurance background preferred. "
            "You will work closely with underwriters to structure tailored coverage solutions."
        ),
        "score_reason": "Strong alignment — B2B SaaS closing experience + cyber degree differentiator.",
    }

    path = write_cover_letter(cv, test_job, force_new_version=True)
    if path:
        print("Saved:", path)
        print()
        print(open(path).read())
