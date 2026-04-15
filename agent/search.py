"""
agent/search.py — Google Custom Search API wrapper.

Generates search queries from TARGET_ROLES x TARGET_LOCATIONS, deduplicates
against URLs already in the Google Sheet, and returns new job listings.
"""

import logging
import requests
from datetime import date
from itertools import product

import config

logger = logging.getLogger(__name__)


def _build_queries(roles: list[str], locations: list[str]) -> list[str]:
    """Return all search query strings for the given roles and locations."""
    queries = []
    for role, location in product(roles, locations):
        queries += [
            f"site:reed.co.uk {role} {location}",
            f"site:adzuna.co.uk {role} {location} apply",
            f"{role} job {location} apply email",
            f"site:jobs.workable.com {role}",
            f"{role} {location} careers apply 2025",
        ]
    return queries


def _google_search(query: str, api_key: str, cse_id: str, num: int = 10) -> list[dict]:
    """Call the Google Custom Search JSON API and return raw items."""
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": api_key,
        "cx": cse_id,
        "q": query,
        "num": min(num, 10),  # API max per request is 10
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("items", [])
    except requests.exceptions.HTTPError as exc:
        logger.error("Google Search HTTP error for query %r: %s", query, exc)
    except requests.exceptions.RequestException as exc:
        logger.error("Google Search request failed for query %r: %s", query, exc)
    except Exception as exc:
        logger.error("Unexpected error during Google Search for query %r: %s", query, exc)
    return []


def _extract_job(item: dict) -> dict:
    """Map a Google Custom Search result item to our job dict schema."""
    return {
        "title": item.get("title", ""),
        "company": _infer_company(item),
        "url": item.get("link", ""),
        "snippet": item.get("snippet", ""),
        "source": _infer_source(item.get("link", "")),
        "date_found": date.today().isoformat(),
    }


def _infer_company(item: dict) -> str:
    """Best-effort company name extraction from search result metadata."""
    # pagemap can contain organisation info
    pagemap = item.get("pagemap", {})
    for key in ("organization", "jobposting"):
        entries = pagemap.get(key, [])
        if entries and isinstance(entries, list):
            name = entries[0].get("name") or entries[0].get("hiringorganization", "")
            if name:
                return name
    # Fall back to the display URL hostname
    display_link = item.get("displayLink", "")
    return display_link.split(".")[0].capitalize() if display_link else ""


def _infer_source(url: str) -> str:
    known = {
        "reed.co.uk": "Reed",
        "adzuna.co.uk": "Adzuna",
        "workable.com": "Workable",
        "linkedin.com": "LinkedIn",
        "totaljobs.com": "TotalJobs",
        "indeed.com": "Indeed",
    }
    for domain, name in known.items():
        if domain in url:
            return name
    return "Web"


def _contains_no_apply_keyword(job: dict) -> bool:
    """Return True if the job should be skipped based on NO_APPLY keywords."""
    combined = (job["title"] + " " + job["snippet"]).lower()
    return any(kw.lower() in combined for kw in config.NO_APPLY)


def search_jobs(existing_urls: set[str]) -> list[dict]:
    """
    Main entry point.  Returns a list of new job dicts not already in
    *existing_urls* and not matching NO_APPLY filters.
    """
    api_key = config.GOOGLE_API_KEY
    cse_id = config.GOOGLE_CSE_ID

    if not api_key or not cse_id:
        logger.error(
            "GOOGLE_API_KEY or GOOGLE_CSE_ID not set — search will return no results."
        )
        return []

    queries = _build_queries(config.TARGET_ROLES, config.TARGET_LOCATIONS)
    seen_urls: set[str] = set(existing_urls)
    new_jobs: list[dict] = []

    for query in queries:
        logger.info("Searching: %s", query)
        items = _google_search(query, api_key, cse_id, num=config.MAX_RESULTS_PER_QUERY)

        for item in items:
            job = _extract_job(item)
            url = job["url"]

            if not url or url in seen_urls:
                continue
            if _contains_no_apply_keyword(job):
                logger.info("Skipping (NO_APPLY match): %s", url)
                continue

            seen_urls.add(url)
            new_jobs.append(job)
            logger.info("Found new job: %s — %s", job["company"], job["title"])

    logger.info("Search complete. %d new jobs found.", len(new_jobs))
    return new_jobs


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    results = search_jobs(existing_urls=set())
    for j in results:
        print(j)
