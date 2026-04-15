"""
main.py — Autonomous job application pipeline orchestrator.

Usage:
    python main.py              # full run
    python main.py --dry-run    # everything except form fill + email send
"""

import argparse
import json
import logging
import pathlib
import sys

# ---------------------------------------------------------------------------
# Logging setup (before any agent imports so all modules use it)
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Imports
# ---------------------------------------------------------------------------
import config  # noqa: E402  (after logging setup)
from agent import search, sheets, score, cover_letter, apply, notify  # noqa: E402

CV_PATH = pathlib.Path(__file__).parent / "data" / "cv.json"


def load_cv() -> dict:
    if not CV_PATH.exists():
        logger.error("cv.json not found at %s", CV_PATH)
        sys.exit(1)
    with open(CV_PATH, encoding="utf-8") as f:
        return json.load(f)


def run(dry_run: bool = False) -> None:
    logger.info("=" * 60)
    logger.info("Job Agent starting — dry_run=%s", dry_run)
    logger.info("=" * 60)

    # 1. Load CV
    cv = load_cv()
    logger.info("CV loaded for: %s", cv.get("full_name", "Unknown"))

    # 2. Connect to Google Sheet
    try:
        sheet = sheets.get_sheet()
        logger.info("Connected to Google Sheet: %s", config.SHEET_NAME)
    except Exception as exc:
        logger.error("Could not connect to Google Sheet: %s", exc)
        sheet = None

    # 3. Get existing URLs to deduplicate
    existing_urls: set[str] = set()
    if sheet:
        existing_urls = sheets.get_existing_urls(sheet)
        logger.info("%d existing URLs in sheet.", len(existing_urls))

    # 4. Search for new jobs
    new_jobs = search.search_jobs(existing_urls)
    logger.info("Found %d new job listings.", len(new_jobs))

    # Counters
    counts = {
        "found": 0,
        "skipped": 0,
        "cover_letters": 0,
        "queued": 0,
        "errors": 0,
    }

    # 5. Process each new job
    for job in new_jobs:
        url = job.get("url", "")

        # 5a. Deduplicate (belt-and-braces check)
        if sheet and sheets.job_exists(sheet, url):
            logger.info("Duplicate — skipping: %s", url)
            continue

        # 5b. Add to sheet (⚪ Found)
        if sheet:
            sheets.add_job(sheet, job)
        counts["found"] += 1
        logger.info("[%d] Processing: %s — %s", counts["found"], job.get("company"), job.get("title"))

        # 5c. Score
        score_result = score.score_job(cv, job, sheet=sheet)
        job_score = score_result.get("score", 0)
        job["score_reason"] = score_result.get("reason", "")
        job["apply_method"] = score_result.get("apply_method", "unknown")
        job["contact_email"] = score_result.get("contact_email") or ""

        # 5d. Skip if below threshold
        if job_score < config.SCORE_THRESHOLD:
            logger.info("Score %d — below threshold, skipping.", job_score)
            counts["skipped"] += 1
            continue

        logger.info("Score %d — proceeding with cover letter.", job_score)

        # 5e. Write cover letter
        cl_path = cover_letter.write_cover_letter(cv, job, sheet=sheet)
        if cl_path:
            counts["cover_letters"] += 1
            job["cover_letter_path"] = cl_path
        else:
            logger.warning("Cover letter generation failed for %s", url)
            counts["errors"] += 1

        # 5f. Form fill (only if apply_method is portal/unknown, not pure email)
        if not dry_run and job.get("apply_method") != "email":
            screenshot = apply.fill_application(cv, job, sheet=sheet)
            if screenshot:
                counts["queued"] += 1
                logger.info("Queued: %s", url)
            else:
                logger.warning("Form fill failed for %s", url)
                counts["errors"] += 1
        elif dry_run:
            logger.info("DRY RUN — skipping form fill for %s", url)

    # 6. Daily digest
    todays_jobs: list[dict] = []
    if sheet:
        todays_jobs = sheets.get_todays_jobs(sheet)

    notify.send_daily_digest(todays_jobs, dry_run=dry_run)

    # 7. Summary
    logger.info("=" * 60)
    logger.info("Run complete.")
    logger.info("  New jobs found:       %d", counts["found"])
    logger.info("  Skipped (low score):  %d", counts["skipped"])
    logger.info("  Cover letters:        %d", counts["cover_letters"])
    logger.info("  Queued for review:    %d", counts["queued"])
    logger.info("  Errors:               %d", counts["errors"])
    logger.info("=" * 60)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Autonomous job application agent")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run everything except form filling and email sending.",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
