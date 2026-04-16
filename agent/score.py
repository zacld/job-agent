"""
agent/score.py — Score a job listing against the candidate's CV using Claude.

Enhancements:
  - Uses full job description text (not just snippet) when available
  - Extracts salary range from description text
  - Returns market_alignment: above_target | within_target | below_target | unknown
  - Auto-skips jobs below SALARY_MIN regardless of role fit score
  - Wrapped with retry_anthropic for resilience
"""

import json
import logging
import re

import anthropic

import config
from agent.retry import retry_anthropic
from agent.sheets import STATUS, update_status

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a job matching agent. Score this job listing for the candidate. "
    "Return ONLY valid JSON, no markdown, no code fences, no extra text."
)

SCORE_SCHEMA = """{
  "score": <int 1-10>,
  "reason": "<2-3 sentences on fit>",
  "apply_method": "<email|portal|unknown>",
  "contact_email": "<email or null>",
  "salary_min": <int or null>,
  "salary_max": <int or null>,
  "salary_currency": "<GBP|USD|EUR|unknown>",
  "market_alignment": "<above_target|within_target|below_target|unknown>",
  "red_flags": ["<string>"],
  "key_matches": ["<string>"]
}"""


# ---------------------------------------------------------------------------
# Salary extraction (regex pre-pass before sending to Claude)
# ---------------------------------------------------------------------------

_SALARY_RE = re.compile(
    r"£\s*([\d,]+)\s*(?:–|-|to)\s*£?\s*([\d,]+)"      # £45,000 – £65,000
    r"|£\s*([\d,]+)k?\s*(?:–|-|to)\s*([\d,]+)k?"       # £45k–£65k
    r"|\b([\d,]+)k?\s*(?:–|-|to)\s*([\d,]+)k?\s*(?:per\s*year|pa|p\.a\.|/yr)",
    re.IGNORECASE,
)


def _extract_salary_hint(text: str) -> str:
    """Return the first salary range found in text, or empty string."""
    m = _SALARY_RE.search(text)
    if not m:
        return ""
    groups = [g for g in m.groups() if g]
    if len(groups) >= 2:
        lo = groups[0].replace(",", "").replace("k", "000")
        hi = groups[1].replace(",", "").replace("k", "000")
        return f"£{lo}–£{hi}"
    return m.group(0)


def _is_below_salary_min(score_result: dict) -> bool:
    """
    Return True if the extracted salary is confidently below SALARY_MIN.
    We only skip if both salary_min and salary_max are below the threshold.
    """
    s_min = score_result.get("salary_min")
    s_max = score_result.get("salary_max")
    currency = score_result.get("salary_currency", "unknown")

    if currency not in ("GBP", "unknown"):
        return False  # Don't skip non-GBP roles on salary grounds

    if s_max is not None and s_max < config.SALARY_MIN:
        return True
    if s_min is not None and s_max is None and s_min < (config.SALARY_MIN * 0.8):
        return True  # Only skip if clearly 20%+ below minimum
    return False


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

@retry_anthropic
def _call_claude(client: anthropic.Anthropic, user_content: str) -> dict:
    response = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    return json.loads(response.content[0].text.strip())


def score_job(cv: dict, job: dict, sheet=None) -> dict:
    """
    Score *job* against *cv* using Claude.

    Uses job['description'] (full JD) when available, falls back to snippet.
    Extracts salary, checks against SALARY_MIN, updates sheet.

    Args:
        cv:    cv.json dict.
        job:   Job dict — needs url, title, company, description or snippet.
        sheet: Optional gspread worksheet.

    Returns:
        Score dict.
    """
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    description = job.get("description") or job.get("snippet", "")
    salary_hint = _extract_salary_hint(description)

    user_content = (
        f"## Candidate CV\n{json.dumps(cv, indent=2)}\n\n"
        f"## Job Listing\n"
        f"Title:    {job.get('title', '')}\n"
        f"Company:  {job.get('company', '')}\n"
        f"URL:      {job.get('url', '')}\n"
        f"Salary hint (pre-extracted): {salary_hint or 'not found in text'}\n"
        f"Candidate salary minimum: £{config.SALARY_MIN:,}\n\n"
        f"Full Job Description:\n{description}\n\n"
        f"## Required Output Schema\n{SCORE_SCHEMA}"
    )

    try:
        result = _call_claude(client, user_content)
    except json.JSONDecodeError as exc:
        logger.error("Claude returned invalid JSON for %s: %s", job.get("url"), exc)
        result = _default_score()
    except Exception as exc:
        logger.error("Scoring failed for %s: %s", job.get("url"), exc)
        result = _default_score()

    score = result.get("score", 0)
    url   = job.get("url", "")
    market = result.get("market_alignment", "unknown")

    # Check salary threshold
    salary_too_low = _is_below_salary_min(result)
    if salary_too_low:
        logger.info(
            "Salary below minimum (£%d) for %s — auto-skipping.",
            config.SALARY_MIN, url,
        )
        if sheet and url:
            update_status(sheet, url, STATUS["skipped"], extras={
                "score": score,
                "score_reason": f"Salary below minimum. {result.get('reason', '')}",
                "apply_method": result.get("apply_method", "unknown"),
                "contact_email": result.get("contact_email") or "",
                "notes": f"Salary: {result.get('salary_min')}–{result.get('salary_max')} ({market})",
            })
        result["_skipped_reason"] = "salary_below_min"
        return result

    # Check score threshold
    if sheet and url:
        if score < config.SCORE_THRESHOLD:
            logger.info("Score %d below threshold — skipping: %s", score, url)
            update_status(sheet, url, STATUS["skipped"], extras={
                "score": score,
                "score_reason": result.get("reason", ""),
                "apply_method": result.get("apply_method", "unknown"),
                "contact_email": result.get("contact_email") or "",
                "notes": f"Market alignment: {market}",
            })
        else:
            logger.info("Score %d — passes threshold: %s", score, url)
            update_status(sheet, url, STATUS["scored"], extras={
                "score": score,
                "score_reason": result.get("reason", ""),
                "apply_method": result.get("apply_method", "unknown"),
                "contact_email": result.get("contact_email") or "",
                "notes": f"Salary: {result.get('salary_min')}–{result.get('salary_max')} ({market})",
            })

    return result


def _default_score() -> dict:
    return {
        "score": 0,
        "reason": "Scoring failed — API or parse error.",
        "apply_method": "unknown",
        "contact_email": None,
        "salary_min": None,
        "salary_max": None,
        "salary_currency": "unknown",
        "market_alignment": "unknown",
        "red_flags": [],
        "key_matches": [],
    }


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import json, pathlib

    logging.basicConfig(level=logging.INFO)

    cv_path = pathlib.Path(__file__).parent.parent / "data" / "cv.json"
    with open(cv_path) as f:
        cv = json.load(f)

    test_job = {
        "title": "Cyber Insurance Sales Executive",
        "company": "Acme Insurers",
        "url": "https://example.com/job/123",
        "description": (
            "We are looking for a commercially driven sales executive to grow our "
            "cyber insurance portfolio. OTE £90,000. Base £55,000–£65,000. "
            "Hybrid — London. B2B technology or insurance sales background preferred. "
            "You will manage the full sales cycle from prospecting to close, working "
            "with CISOs and risk managers at enterprise clients."
        ),
    }

    result = score_job(cv, test_job)
    print(json.dumps(result, indent=2))
