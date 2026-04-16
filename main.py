"""
main.py — Autonomous job application pipeline orchestrator.

Pipeline:
  1. Load CV + config
  2. Connect to Google Sheet
  3. Google Custom Search → new job listings (with full JD text scraping)
  4. LinkedIn scrape (optional, USE_LINKEDIN=true)
  5. For each new job:
       a. Deduplicate (URL-normalised)
       b. Add to sheet (⚪ Found)
       c. Score with Claude (salary check + fit score)
       d. Skip if below threshold
       e. Write tailored cover letter (versioned)
       f. Email apply (if apply_method=email + contact_email known)
       g. Portal form fill via Playwright + Claude Vision (multi-turn)
  6. Follow-up check — draft chase emails for 🟢 Applied jobs 7+ days old
  7. Daily digest email
  8. Console summary

Flags:
  --dry-run     No form fills, no emails sent, no sheet writes beyond scoring
  --skip-search Skip the search step (useful for re-processing existing jobs)
  --linkedin    Force LinkedIn scraping on regardless of USE_LINKEDIN env var
"""

import argparse
import json
import logging
import pathlib
import sys

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Imports (after logging setup)
# ---------------------------------------------------------------------------
import config
from agent import sheets
from agent.search       import search_jobs
from agent.score        import score_job
from agent.cover_letter import write_cover_letter
from agent.apply        import fill_application
from agent.email_apply  import send_email_application
from agent.followup     import check_and_send_followups
from agent.notify       import send_daily_digest

CV_PATH = pathlib.Path(__file__).parent / "data" / "cv.json"


def load_cv() -> dict:
    if not CV_PATH.exists():
        logger.error("cv.json not found at %s", CV_PATH)
        sys.exit(1)
    with open(CV_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def run(
    dry_run: bool = False,
    skip_search: bool = False,
    use_linkedin: bool = False,
    roles: list[str] | None = None,
) -> None:
    logger.info("=" * 64)
    logger.info("Job Agent — dry_run=%s | linkedin=%s", dry_run, use_linkedin)
    if roles:
        logger.info("Custom roles: %s", roles)
    logger.info("=" * 64)

    cv = load_cv()
    logger.info("CV: %s", cv.get("full_name"))

    # ── Sheet connection ────────────────────────────────────────────────────
    sheet = None
    try:
        sheet = sheets.get_sheet()
        logger.info("Sheet connected: %s", config.SHEET_NAME)
    except Exception as exc:
        logger.warning("Sheet unavailable: %s — continuing without sheet.", exc)

    existing_urls = sheets.get_existing_urls(sheet) if sheet else set()
    logger.info("%d URLs already tracked.", len(existing_urls))

    # ── Search ──────────────────────────────────────────────────────────────
    new_jobs: list[dict] = []

    if not skip_search:
        # Google Custom Search
        google_jobs = search_jobs(existing_urls, roles=roles)
        new_jobs.extend(google_jobs)

        # LinkedIn (optional)
        if use_linkedin or config.USE_LINKEDIN:
            try:
                from agent.linkedin import scrape_linkedin_jobs
                li_jobs = scrape_linkedin_jobs(existing_urls)
                new_jobs.extend(li_jobs)
                logger.info("LinkedIn added %d jobs.", len(li_jobs))
            except Exception as exc:
                logger.error("LinkedIn scrape failed: %s", exc)

        # Deduplicate across both sources
        seen: set[str] = set()
        deduped: list[dict] = []
        for job in new_jobs:
            key = job.get("url_norm") or job.get("url", "")
            if key and key not in seen:
                seen.add(key)
                deduped.append(job)
        new_jobs = deduped

    logger.info("%d new jobs to process.", len(new_jobs))

    # ── Process each job ────────────────────────────────────────────────────
    counts = {"found": 0, "skipped": 0, "cover": 0, "emailed": 0, "queued": 0, "errors": 0}

    for job in new_jobs:
        url = job.get("url", "")

        # Belt-and-braces dedupe against sheet
        if sheet and sheets.job_exists(sheet, url):
            logger.debug("Duplicate in sheet — skipping: %s", url)
            continue

        # Add to sheet
        if sheet and not dry_run:
            sheets.add_job(sheet, job)
        counts["found"] += 1

        label = f"[{counts['found']}] {job.get('company','?')} — {job.get('title','?')}"
        logger.info(label)

        # Score
        score_result = score_job(cv, job, sheet=sheet if not dry_run else None)
        job_score    = score_result.get("score", 0)

        # Copy scoring data back onto job dict for downstream use
        job["score_reason"]  = score_result.get("reason", "")
        job["apply_method"]  = score_result.get("apply_method", "unknown")
        job["contact_email"] = score_result.get("contact_email") or ""

        # Skip?
        if (
            job_score < config.SCORE_THRESHOLD
            or score_result.get("_skipped_reason")
        ):
            reason = score_result.get("_skipped_reason", "low_score")
            logger.info("  ⏭ Skipping (%s, score=%d)", reason, job_score)
            counts["skipped"] += 1
            continue

        logger.info("  ✓ Score %d — writing cover letter…", job_score)

        # Cover letter
        cl_path = write_cover_letter(cv, job, sheet=sheet if not dry_run else None)
        if cl_path:
            counts["cover"] += 1
            job["cover_letter_path"] = cl_path
        else:
            logger.warning("  Cover letter failed for %s", url)
            counts["errors"] += 1
            continue

        if dry_run:
            logger.info("  DRY RUN — skipping application step.")
            continue

        # Email application
        if job["apply_method"] == "email" and job["contact_email"]:
            logger.info("  📧 Sending email application…")
            sent = send_email_application(cv, job, cl_path, sheet=sheet)
            if sent:
                counts["emailed"] += 1
            else:
                counts["errors"] += 1

        # Portal form fill
        elif job["apply_method"] in ("portal", "unknown"):
            logger.info("  🖥  Filling application form…")
            screenshot = fill_application(cv, job, sheet=sheet)
            if screenshot:
                counts["queued"] += 1
            else:
                counts["errors"] += 1

    # ── Follow-up check ─────────────────────────────────────────────────────
    followup_jobs: list[dict] = []
    if sheet:
        logger.info("Checking for follow-ups due…")
        followup_jobs = check_and_send_followups(
            cv, sheet,
            auto_send=config.AUTO_SEND_FOLLOWUPS,
            dry_run=dry_run,
        )
        if followup_jobs:
            logger.info("%d follow-up(s) drafted.", len(followup_jobs))

    # ── Daily digest ────────────────────────────────────────────────────────
    todays_jobs: list[dict] = []
    if sheet:
        todays_jobs = sheets.get_todays_jobs(sheet)

    # Add follow-up flag to digest jobs
    followup_urls = {j.get("Source URL") for j in followup_jobs}
    for j in todays_jobs:
        if j.get("Source URL") in followup_urls:
            j["_needs_followup"] = True

    send_daily_digest(todays_jobs, dry_run=dry_run)

    # ── Summary ─────────────────────────────────────────────────────────────
    logger.info("=" * 64)
    logger.info("Run complete.")
    logger.info("  New jobs found:        %d", counts["found"])
    logger.info("  Skipped:               %d", counts["skipped"])
    logger.info("  Cover letters written: %d", counts["cover"])
    logger.info("  Email applications:    %d", counts["emailed"])
    logger.info("  Portal (queued):       %d", counts["queued"])
    logger.info("  Follow-ups drafted:    %d", len(followup_jobs))
    logger.info("  Errors:                %d", counts["errors"])
    logger.info("=" * 64)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Autonomous job application agent")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Run without form fills, emails, or sheet writes beyond scoring.",
    )
    parser.add_argument(
        "--skip-search", action="store_true",
        help="Skip search step — re-process any unprocessed jobs in the sheet.",
    )
    parser.add_argument(
        "--linkedin", action="store_true",
        help="Enable LinkedIn scraping (overrides USE_LINKEDIN env var).",
    )
    parser.add_argument(
        "--roles",
        help="Comma-separated job titles to search for (overrides config.TARGET_ROLES).",
        default="",
    )
    args = parser.parse_args()
    custom_roles = [r.strip() for r in args.roles.split(",") if r.strip()] if args.roles else None
    run(
        dry_run=args.dry_run,
        skip_search=args.skip_search,
        use_linkedin=args.linkedin,
        roles=custom_roles,
    )


if __name__ == "__main__":
    main()
