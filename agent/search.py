"""
agent/search.py — Google Custom Search API wrapper.

Generates search queries from TARGET_ROLES x TARGET_LOCATIONS, deduplicates
against URLs already in the Google Sheet (using normalised URLs), respects
the 100-query/day free-tier quota, and enriches each result with the full
job description text via agent.scraper.
"""

import hashlib
import logging
import re
from datetime import date
from itertools import product
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

import requests

import config
from agent.retry import google_quota, google_limiter, retry_google

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# URL normalisation
# ---------------------------------------------------------------------------

# Query params that are tracking/session noise — strip these
_STRIP_PARAMS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "ref", "source", "from", "origin", "fbclid", "gclid", "msclkid",
    "trk", "trkInfo", "trackingId", "sid", "session", "token",
})


def normalise_url(url: str) -> str:
    """
    Normalise a URL for deduplication:
    - Lowercase scheme + host
    - Strip tracking query params
    - Remove trailing slash from path
    - Drop fragment (#)
    """
    try:
        parsed = urlparse(url.strip().lower())
        path = parsed.path.rstrip("/") or "/"
        params = {
            k: v for k, v in parse_qs(parsed.query).items()
            if k not in _STRIP_PARAMS
        }
        clean_query = urlencode({k: v[0] for k, v in sorted(params.items())})
        normalised = urlunparse((
            parsed.scheme, parsed.netloc, path,
            parsed.params, clean_query, ""
        ))
        return normalised
    except Exception:
        return url.lower().strip()


def url_fingerprint(url: str) -> str:
    """Short MD5 fingerprint of a normalised URL — used as a secondary dedup key."""
    return hashlib.md5(normalise_url(url).encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Query generation
# ---------------------------------------------------------------------------

def _build_queries(roles: list[str], locations: list[str]) -> list[str]:
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


# ---------------------------------------------------------------------------
# Google Custom Search call
# ---------------------------------------------------------------------------

@google_limiter
@retry_google
def _google_search(query: str, api_key: str, cse_id: str, num: int = 10) -> list[dict]:
    if not google_quota.check_and_increment():
        return []
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": api_key,
        "cx": cse_id,
        "q": query,
        "num": min(num, 10),
    }
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 429:
            raise Exception("Google API rate limit hit")
        resp.raise_for_status()
        return resp.json().get("items", [])
    except requests.exceptions.HTTPError as exc:
        logger.error("Google Search HTTP error for %r: %s", query, exc)
        return []
    except Exception as exc:
        logger.error("Google Search error for %r: %s", query, exc)
        return []


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

def _extract_job(item: dict) -> dict:
    return {
        "title":      item.get("title", ""),
        "company":    _infer_company(item),
        "url":        item.get("link", ""),
        "url_norm":   normalise_url(item.get("link", "")),
        "snippet":    item.get("snippet", ""),
        "source":     _infer_source(item.get("link", "")),
        "date_found": date.today().isoformat(),
        "description": "",  # filled by scraper.enrich_jobs()
    }


def _infer_company(item: dict) -> str:
    pagemap = item.get("pagemap", {})
    for key in ("organization", "jobposting"):
        entries = pagemap.get(key, [])
        if entries and isinstance(entries, list):
            name = entries[0].get("name") or entries[0].get("hiringorganization", "")
            if name:
                return name
    display_link = item.get("displayLink", "")
    return display_link.split(".")[0].capitalize() if display_link else ""


def _infer_source(url: str) -> str:
    known = {
        "reed.co.uk":     "Reed",
        "adzuna.co.uk":   "Adzuna",
        "workable.com":   "Workable",
        "linkedin.com":   "LinkedIn",
        "totaljobs.com":  "TotalJobs",
        "indeed.com":     "Indeed",
    }
    for domain, name in known.items():
        if domain in url:
            return name
    return "Web"


def _contains_no_apply_keyword(job: dict) -> bool:
    combined = (job["title"] + " " + job["snippet"]).lower()
    return any(kw.lower() in combined for kw in config.NO_APPLY)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def search_jobs(existing_urls: set[str]) -> list[dict]:
    """
    Search Google for new job listings, deduplicate against *existing_urls*
    (supports both raw and normalised URLs), enrich with full JD text.

    Returns list of new job dicts.
    """
    api_key = config.GOOGLE_API_KEY
    cse_id  = config.GOOGLE_CSE_ID

    if not api_key or not cse_id:
        logger.error("GOOGLE_API_KEY or GOOGLE_CSE_ID not set.")
        return []

    # Build normalised set for deduplication
    norm_existing = {normalise_url(u) for u in existing_urls if u}

    queries = _build_queries(config.TARGET_ROLES, config.TARGET_LOCATIONS)
    seen_norm: set[str] = set(norm_existing)
    new_jobs: list[dict] = []

    logger.info("Quota remaining: %d/95 Google searches", google_quota.remaining)

    for query in queries:
        if google_quota.remaining == 0:
            logger.warning("Daily quota exhausted — stopping search early.")
            break

        logger.info("Searching: %s", query)
        items = _google_search(query, api_key, cse_id, num=config.MAX_RESULTS_PER_QUERY)

        for item in items:
            job = _extract_job(item)
            url_norm = job["url_norm"]

            if not job["url"] or url_norm in seen_norm:
                continue
            if _contains_no_apply_keyword(job):
                logger.info("Skipping (NO_APPLY): %s", job["url"])
                continue

            seen_norm.add(url_norm)
            new_jobs.append(job)
            logger.info("Found: %s — %s", job["company"], job["title"])

    # Enrich with full JD text
    if new_jobs:
        logger.info("Fetching full job descriptions for %d listings…", len(new_jobs))
        from agent.scraper import enrich_jobs
        new_jobs = enrich_jobs(new_jobs, delay=1.2)

    logger.info("Search complete. %d new jobs found.", len(new_jobs))
    return new_jobs


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    results = search_jobs(existing_urls=set())
    for j in results:
        print(j["company"], "—", j["title"])
        print("  Description preview:", j["description"][:200])
        print()
