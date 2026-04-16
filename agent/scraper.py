"""
agent/scraper.py — Full job description text fetcher.

After Google Custom Search returns a URL + snippet, this module follows the
URL and extracts the complete job description text so Claude has real content
to work with (not a 150-char snippet).

Supports:
  - Generic HTML scraping via BeautifulSoup
  - Reed.co.uk structured extraction
  - Adzuna structured extraction
  - Workable job pages
  - Fallback: return snippet if scrape fails
"""

import logging
import re
import time
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

REQUEST_TIMEOUT = 15
MIN_TEXT_LENGTH = 200  # chars — below this we treat it as a failed scrape


# ---------------------------------------------------------------------------
# Source-specific extractors
# ---------------------------------------------------------------------------

def _extract_reed(soup: BeautifulSoup) -> str:
    """Reed.co.uk job description extraction."""
    selectors = [
        {"itemprop": "description"},
        {"class": re.compile(r"job-description", re.I)},
        {"id": re.compile(r"job-description", re.I)},
    ]
    for attrs in selectors:
        el = soup.find(attrs=attrs)
        if el:
            return el.get_text(separator="\n", strip=True)
    return ""


def _extract_adzuna(soup: BeautifulSoup) -> str:
    """Adzuna job description extraction."""
    selectors = [
        {"class": re.compile(r"adz-job-description|job-description", re.I)},
        {"data-testid": "job-description"},
    ]
    for attrs in selectors:
        el = soup.find(attrs=attrs)
        if el:
            return el.get_text(separator="\n", strip=True)
    return ""


def _extract_workable(soup: BeautifulSoup) -> str:
    """Workable job pages."""
    el = soup.find("section", {"class": re.compile(r"job-description|description", re.I)})
    if el:
        return el.get_text(separator="\n", strip=True)
    return ""


def _extract_linkedin(soup: BeautifulSoup) -> str:
    """LinkedIn job description (public view)."""
    el = soup.find("div", {"class": re.compile(r"description__text|show-more-less-html", re.I)})
    if el:
        return el.get_text(separator="\n", strip=True)
    return ""


def _extract_generic(soup: BeautifulSoup) -> str:
    """
    Heuristic extraction for any unknown job board.
    Finds the largest block of text that looks like a job description.
    """
    # Remove boilerplate tags
    for tag in soup(["script", "style", "nav", "header", "footer",
                     "aside", "form", "iframe", "noscript"]):
        tag.decompose()

    # Try common job description containers by priority
    candidates = [
        soup.find(attrs={"class": re.compile(r"job.?desc|description|vacancy|posting|detail", re.I)}),
        soup.find(attrs={"id": re.compile(r"job.?desc|description|vacancy|posting|detail", re.I)}),
        soup.find("article"),
        soup.find("main"),
    ]

    for el in candidates:
        if el:
            text = el.get_text(separator="\n", strip=True)
            if len(text) >= MIN_TEXT_LENGTH:
                return text

    # Last resort: biggest <div> by text length
    divs = soup.find_all("div")
    if divs:
        best = max(divs, key=lambda d: len(d.get_text(strip=True)), default=None)
        if best:
            text = best.get_text(separator="\n", strip=True)
            if len(text) >= MIN_TEXT_LENGTH:
                return text

    return ""


def _clean_text(text: str) -> str:
    """Normalise whitespace and remove excessive blank lines."""
    lines = [line.strip() for line in text.splitlines()]
    # Collapse 3+ blank lines → 2
    cleaned = []
    blanks = 0
    for line in lines:
        if not line:
            blanks += 1
            if blanks <= 2:
                cleaned.append("")
        else:
            blanks = 0
            cleaned.append(line)
    return "\n".join(cleaned).strip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_job_description(url: str, snippet: str = "") -> str:
    """
    Fetch and return the full job description text for *url*.

    Falls back to *snippet* if the page cannot be scraped successfully.

    Args:
        url:     Job listing URL.
        snippet: Google snippet — used as fallback.

    Returns:
        Full job description string (or snippet on failure).
    """
    if not url:
        return snippet

    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as exc:
        logger.warning("HTTP error fetching %s: %s — using snippet", url, exc)
        return snippet
    except requests.exceptions.RequestException as exc:
        logger.warning("Request failed for %s: %s — using snippet", url, exc)
        return snippet

    soup = BeautifulSoup(resp.text, "lxml")
    domain = urlparse(url).netloc.lower()

    # Source-specific extractors
    if "reed.co.uk" in domain:
        text = _extract_reed(soup)
    elif "adzuna.co.uk" in domain or "adzuna.com" in domain:
        text = _extract_adzuna(soup)
    elif "workable.com" in domain or "jobs.workable.com" in domain:
        text = _extract_workable(soup)
    elif "linkedin.com" in domain:
        text = _extract_linkedin(soup)
    else:
        text = _extract_generic(soup)

    # If source-specific extractor returned too little, try generic
    if len(text) < MIN_TEXT_LENGTH:
        text = _extract_generic(soup)

    # If still too short, fall back to snippet
    if len(text) < MIN_TEXT_LENGTH:
        logger.warning("Scrape returned too little text for %s — using snippet", url)
        return snippet

    cleaned = _clean_text(text)
    logger.info("Scraped %d chars from %s", len(cleaned), url)
    return cleaned


def enrich_jobs(jobs: list[dict], delay: float = 1.0) -> list[dict]:
    """
    Add a 'description' key to each job dict with the full JD text.

    Args:
        jobs:  List of job dicts (must have 'url' and 'snippet').
        delay: Seconds to wait between requests (be polite).

    Returns:
        Same list with 'description' populated.
    """
    for i, job in enumerate(jobs):
        url = job.get("url", "")
        snippet = job.get("snippet", "")
        logger.info("[%d/%d] Fetching JD: %s", i + 1, len(jobs), url)
        job["description"] = fetch_job_description(url, snippet)
        if i < len(jobs) - 1:
            time.sleep(delay)
    return jobs


# ---------------------------------------------------------------------------
# Standalone test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test_url = "https://www.reed.co.uk/jobs/cyber-security-sales-executive/1234567"
    desc = fetch_job_description(test_url, snippet="Test fallback snippet.")
    print(desc[:500])
