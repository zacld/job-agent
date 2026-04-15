"""
agent/score.py — Score a job listing against the candidate's CV using Claude.

Returns a structured score dict and updates the Google Sheet accordingly.
"""

import json
import logging

import anthropic

import config
from agent.sheets import STATUS, update_status

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a job matching agent. Score this job listing for the candidate. "
    "Return ONLY valid JSON, no markdown, no code fences, no extra text."
)

SCORE_SCHEMA = """{
  "score": <int 1-10>,
  "reason": "<string — 2-3 sentences explaining fit>",
  "apply_method": "<email|portal|unknown>",
  "contact_email": "<email address or null>",
  "red_flags": ["<string>", ...],
  "key_matches": ["<string>", ...]
}"""


def score_job(cv: dict, job: dict, sheet=None) -> dict:
    """
    Call Claude to score *job* against *cv*.

    Args:
        cv:    Loaded cv.json dict.
        job:   Job dict with keys: title, company, url, snippet.
        sheet: Optional gspread worksheet — if supplied, status is updated.

    Returns:
        Score dict (see SCORE_SCHEMA above).  On error returns a default
        dict with score=0 so the pipeline can continue.
    """
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

    user_content = (
        f"## Candidate CV\n{json.dumps(cv, indent=2)}\n\n"
        f"## Job Listing\n"
        f"Title: {job.get('title', '')}\n"
        f"Company: {job.get('company', '')}\n"
        f"URL: {job.get('url', '')}\n"
        f"Description / Snippet:\n{job.get('snippet', '')}\n\n"
        f"## Required Output Schema\n{SCORE_SCHEMA}"
    )

    try:
        response = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = response.content[0].text.strip()
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Claude returned invalid JSON for %s: %s", job.get("url"), exc)
        result = _default_score()
    except anthropic.APIError as exc:
        logger.error("Claude API error scoring %s: %s", job.get("url"), exc)
        result = _default_score()
    except Exception as exc:
        logger.error("Unexpected error scoring %s: %s", job.get("url"), exc)
        result = _default_score()

    score = result.get("score", 0)
    url = job.get("url", "")

    if sheet and url:
        if score < config.SCORE_THRESHOLD:
            logger.info("Score %d below threshold — skipping: %s", score, url)
            update_status(
                sheet, url, STATUS["skipped"],
                extras={
                    "score": score,
                    "score_reason": result.get("reason", ""),
                    "apply_method": result.get("apply_method", "unknown"),
                    "contact_email": result.get("contact_email") or "",
                },
            )
        else:
            logger.info("Score %d — job passes threshold: %s", score, url)
            update_status(
                sheet, url, STATUS["scored"],
                extras={
                    "score": score,
                    "score_reason": result.get("reason", ""),
                    "apply_method": result.get("apply_method", "unknown"),
                    "contact_email": result.get("contact_email") or "",
                },
            )

    return result


def _default_score() -> dict:
    return {
        "score": 0,
        "reason": "Scoring failed — API or parse error.",
        "apply_method": "unknown",
        "contact_email": None,
        "red_flags": [],
        "key_matches": [],
    }


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import pathlib

    logging.basicConfig(level=logging.INFO)

    cv_path = pathlib.Path(__file__).parent.parent / "data" / "cv.json"
    with open(cv_path) as f:
        cv = json.load(f)

    test_job = {
        "title": "Cyber Insurance Sales Executive",
        "company": "Acme Insurers",
        "url": "https://example.com/job/123",
        "snippet": (
            "We are looking for a commercially driven sales executive to grow our "
            "cyber insurance book. OTE £90k. Hybrid — London. Experience in B2B "
            "technology or insurance sales preferred."
        ),
    }

    result = score_job(cv, test_job)
    print(json.dumps(result, indent=2))
