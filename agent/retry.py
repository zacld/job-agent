"""
agent/retry.py — Rate limiting, retry decorators, and quota tracking.

All external API calls (Google Custom Search, Anthropic, Gmail) should be
wrapped using the decorators or helpers defined here.

Usage:
    from agent.retry import retry_anthropic, retry_google, rate_limited

    @retry_anthropic
    def call_claude(...):
        ...

    @retry_google
    def call_search(...):
        ...
"""

import functools
import logging
import time
from collections import deque
from datetime import date
from threading import Lock

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)

try:
    import anthropic as _anthropic
    _ANTHROPIC_ERRORS = (
        _anthropic.RateLimitError,
        _anthropic.APIConnectionError,
        _anthropic.InternalServerError,
    )
except ImportError:
    _ANTHROPIC_ERRORS = (Exception,)

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Anthropic retry — 3 attempts, 4s → 60s exponential backoff
# ---------------------------------------------------------------------------

retry_anthropic = retry(
    retry=retry_if_exception_type(_ANTHROPIC_ERRORS),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(3),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)

# ---------------------------------------------------------------------------
# Google Custom Search retry — also catches HTTP 429 / 500
# ---------------------------------------------------------------------------

class _GoogleAPIError(Exception):
    """Raised when Google Search returns a retryable error code."""


def retry_google(func):
    """Decorator: retry Google API calls on transient errors."""
    @functools.wraps(func)
    @retry(
        retry=retry_if_exception_type((_GoogleAPIError, requests.exceptions.ConnectionError)),
        wait=wait_exponential(multiplier=2, min=5, max=90),
        stop=stop_after_attempt(3),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        reraise=False,
    )
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    return wrapper


# ---------------------------------------------------------------------------
# Google Custom Search quota tracker
# (Free tier: 100 queries/day)
# ---------------------------------------------------------------------------

class _QuotaTracker:
    """Thread-safe daily quota counter."""

    def __init__(self, daily_limit: int = 95):  # leave 5 buffer
        self._limit = daily_limit
        self._count = 0
        self._day = date.today()
        self._lock = Lock()

    def _reset_if_new_day(self):
        today = date.today()
        if today != self._day:
            self._count = 0
            self._day = today

    def check_and_increment(self) -> bool:
        """Returns True if quota is available and increments the counter."""
        with self._lock:
            self._reset_if_new_day()
            if self._count >= self._limit:
                logger.warning(
                    "Google Custom Search daily quota reached (%d/%d). "
                    "Skipping further searches today.",
                    self._count, self._limit,
                )
                return False
            self._count += 1
            logger.debug("Google quota used: %d/%d", self._count, self._limit)
            return True

    @property
    def remaining(self) -> int:
        with self._lock:
            self._reset_if_new_day()
            return max(0, self._limit - self._count)


google_quota = _QuotaTracker(daily_limit=95)


# ---------------------------------------------------------------------------
# Generic scrape rate limiter (politeness delay between requests)
# ---------------------------------------------------------------------------

class _RateLimiter:
    """
    Sliding-window rate limiter.
    Ensures at most *max_calls* calls per *period* seconds.
    """

    def __init__(self, max_calls: int, period: float):
        self._max = max_calls
        self._period = period
        self._calls: deque[float] = deque()
        self._lock = Lock()

    def __call__(self, func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with self._lock:
                now = time.monotonic()
                # Drop timestamps outside the window
                while self._calls and now - self._calls[0] > self._period:
                    self._calls.popleft()
                if len(self._calls) >= self._max:
                    sleep_for = self._period - (now - self._calls[0])
                    if sleep_for > 0:
                        logger.debug("Rate limiter: sleeping %.1fs", sleep_for)
                        time.sleep(sleep_for)
                self._calls.append(time.monotonic())
            return func(*args, **kwargs)
        return wrapper


# 2 scrape requests per second max
scrape_limiter = _RateLimiter(max_calls=2, period=1.0)

# 1 Google API call per 2 seconds max (on top of quota tracking)
google_limiter = _RateLimiter(max_calls=1, period=2.0)


# ---------------------------------------------------------------------------
# Convenience wrapper: rate-limited + retried scrape request
# ---------------------------------------------------------------------------

@scrape_limiter
def safe_get(url: str, **kwargs) -> requests.Response | None:
    """
    GET *url* with retry on transient network errors.
    Returns the Response or None on failure.
    """
    headers = kwargs.pop("headers", {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    })
    timeout = kwargs.pop("timeout", 15)
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=timeout,
                                allow_redirects=True, **kwargs)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 10))
                logger.warning("429 from %s — waiting %ds", url, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            if attempt == 2:
                logger.error("safe_get failed after 3 attempts for %s: %s", url, exc)
                return None
            backoff = 2 ** attempt
            logger.warning("Attempt %d failed for %s — retrying in %ds", attempt + 1, url, backoff)
            time.sleep(backoff)
    return None
