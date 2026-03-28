"""
wikisource_fetcher.py — Wikisource / Wikimedia API Client

Fetches page content from Wikisource (en.wikisource.org) using the official
Wikimedia REST API.  Includes:
  - Rate-limiting (configurable delay between requests)
  - Prefix-search so candidates can be fed into SmartSearchEngine
  - Bulk processing pipeline (sequential with progress callbacks)
  - Robust error handling for timeouts, HTTP errors, and missing pages

Public API
----------
WikisourceFetcher(rate_limit_seconds=1.0, timeout=15)
    .search_titles(query: str) -> list[str]
    .fetch_page(title: str) -> PageResult
    .fetch_bulk(titles: list[str], callback) -> list[PageResult]
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

from config import WIKISOURCE_API_URL, WIKISOURCE_SEARCH_LIMIT, DEFAULT_RATE_LIMIT_SECONDS
from smart_search import SmartSearchEngine

logger = logging.getLogger(__name__)

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "GitaVyasa/1.0 (https://github.com/srinidhi2608/GitaVyasa; educational-use) python-requests"
})


@dataclass
class PageResult:
    """Outcome of a single page-fetch attempt."""
    query: str                        # original user query
    title: str                        # resolved Wikisource page title (may differ)
    url: str                          # canonical URL
    content: str                      # plain-text wikitext content
    match_method: str                 # "exact" | "alias" | "phonetic" | "fuzzy" | "normalised"
    match_score: float                # 0–100
    tried_variants: list[str] = field(default_factory=list)
    success: bool = True
    error: str = ""


class WikisourceFetcher:
    """
    Fetches pages from English Wikisource with smart-search fallback.

    Parameters
    ----------
    rate_limit_seconds : float
        Minimum seconds to wait between consecutive API calls.
    timeout : int
        HTTP request timeout in seconds.
    """

    def __init__(
        self,
        rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
        timeout: int = 15,
    ):
        self.rate_limit = rate_limit_seconds
        self.timeout = timeout
        self._last_request_time: float = 0.0

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def search_titles(self, query: str, limit: int = WIKISOURCE_SEARCH_LIMIT) -> list[str]:
        """
        Use the Wikisource search API (srnamespace=0) to get candidate titles.

        Returns a list of page titles matching *query*.
        """
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srnamespace": "0",
            "srlimit": str(limit),
            "format": "json",
        }
        try:
            data = self._get(WIKISOURCE_API_URL, params)
            return [item["title"] for item in data.get("query", {}).get("search", [])]
        except Exception as exc:
            logger.warning("search_titles failed for '%s': %s", query, exc)
            return []

    def fetch_page(self, query: str) -> PageResult:
        """
        Fetch the Wikisource page best matching *query*.

        1. Search Wikisource for candidate titles.
        2. Pass candidates to SmartSearchEngine.find_best_match().
        3. Retrieve the wikitext of the matched page.
        """
        logger.info("Fetching: '%s'", query)

        # Step 1 – get candidates from Wikisource search
        candidates = self.search_titles(query)
        logger.debug("Candidates for '%s': %s", query, candidates)

        # Step 2 – smart-match
        engine = SmartSearchEngine(candidates)
        match = engine.find_best_match(query)

        if match is None:
            return PageResult(
                query=query,
                title="",
                url="",
                content="",
                match_method="none",
                match_score=0.0,
                tried_variants=engine.generate_variants(query),
                success=False,
                error="No matching page found on Wikisource",
            )

        # Step 3 – fetch wikitext
        content, url = self._fetch_wikitext(match.matched_title)
        if content is None:
            return PageResult(
                query=query,
                title=match.matched_title,
                url=url,
                content="",
                match_method=match.method,
                match_score=match.score,
                tried_variants=match.tried,
                success=False,
                error=f"Page '{match.matched_title}' found but content could not be retrieved",
            )

        return PageResult(
            query=query,
            title=match.matched_title,
            url=url,
            content=content,
            match_method=match.method,
            match_score=match.score,
            tried_variants=match.tried,
            success=True,
        )

    def fetch_bulk(
        self,
        queries: list[str],
        progress_callback: Optional[Callable[[int, int, PageResult], None]] = None,
    ) -> list[PageResult]:
        """
        Process a list of source names sequentially with rate-limiting.

        Parameters
        ----------
        queries : list[str]
            Source names to fetch.
        progress_callback : callable, optional
            Called after each item as ``callback(current_index, total, result)``.
            Useful for updating a UI progress bar.

        Returns
        -------
        list[PageResult]
        """
        results: list[PageResult] = []
        total = len(queries)
        for idx, query in enumerate(queries, start=1):
            result = self.fetch_page(query.strip())
            results.append(result)
            if progress_callback:
                progress_callback(idx, total, result)
        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _throttle(self) -> None:
        """Enforce the rate limit between API calls."""
        elapsed = time.monotonic() - self._last_request_time
        wait = self.rate_limit - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_time = time.monotonic()

    def _get(self, url: str, params: dict) -> dict:
        """Execute a throttled GET request and return the JSON response body."""
        self._throttle()
        response = _SESSION.get(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        return response.json()

    def _fetch_wikitext(self, title: str) -> tuple[Optional[str], str]:
        """
        Retrieve the plain wikitext of a Wikisource page.

        Returns (content, url) where content is None on failure.
        """
        url = f"https://en.wikisource.org/wiki/{title.replace(' ', '_')}"
        params = {
            "action": "query",
            "titles": title,
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "format": "json",
        }
        try:
            data = self._get(WIKISOURCE_API_URL, params)
            pages = data.get("query", {}).get("pages", {})
            for page_data in pages.values():
                if "missing" in page_data:
                    logger.warning("Page '%s' is marked missing", title)
                    return None, url
                slots = page_data.get("revisions", [{}])[0].get("slots", {})
                content = slots.get("main", {}).get("*", "")
                if content:
                    return content, url
            return None, url
        except requests.exceptions.Timeout:
            logger.error("Timeout fetching '%s'", title)
            return None, url
        except requests.exceptions.RequestException as exc:
            logger.error("Request error for '%s': %s", title, exc)
            return None, url
