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
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

from config import WIKISOURCE_API_URL, WIKISOURCE_SEARCH_LIMIT, DEFAULT_RATE_LIMIT_SECONDS
from smart_search import SmartSearchEngine

try:
    from rapidfuzz import fuzz as _fuzz  # type: ignore
    _RAPIDFUZZ_AVAILABLE = True
except ImportError:
    _RAPIDFUZZ_AVAILABLE = False

logger = logging.getLogger(__name__)

_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "GitaVyasa/1.0 (https://github.com/srinidhi2608/GitaVyasa; educational-use) python-requests"
})

_SNIPPET_ALLOWED_RE = re.compile(r"<(?!/?span\b)[^>]+>")


def _clean_snippet(html: str) -> str:
    """
    Lightly sanitise a Wikisource search snippet for display.

    Keeps <span class="searchmatch"> tags (used for highlighting) and strips
    everything else.
    """
    return _SNIPPET_ALLOWED_RE.sub("", html).strip()


@dataclass
class CandidateInfo:
    """A single ranked match candidate for a user query, ready for UI display."""
    title: str
    snippet: str        # sanitised HTML excerpt from Wikisource search
    url: str            # canonical Wikisource page URL
    match_method: str   # "exact" | "alias" | "normalised" | "phonetic" | "fuzzy" | "search"
    match_score: float  # 0–100


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
        return [h["title"] for h in self._search_with_snippets(query, limit=limit)]

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

    def search_candidates(
        self,
        query: str,
        top_n: int = 6,
        score_threshold: float = 40.0,
    ) -> list[CandidateInfo]:
        """
        Return a ranked list of Wikisource page candidates for *query*.

        Steps:
        1. Fetch raw search hits (with HTML snippets) from the Wikisource API.
        2. Score every hit against *query* using RapidFuzz (if available).
        3. Run SmartSearchEngine to identify the best semantic match; promote
           it to the top of the list regardless of raw search rank.
        4. Return up to *top_n* candidates, deduplicated.

        Parameters
        ----------
        query : str
            The user's original (possibly variant-spelled) query.
        top_n : int
            Maximum number of candidates to return.
        score_threshold : float
            Minimum fuzzy score (0–100) to include a search result.
        """
        raw_hits = self._search_with_snippets(query, limit=WIKISOURCE_SEARCH_LIMIT)
        titles = [h["title"] for h in raw_hits]
        snippet_map = {h["title"]: _clean_snippet(h.get("snippet", "")) for h in raw_hits}

        # Smart-match: identify the best candidate across all tiers
        engine = SmartSearchEngine(titles, score_threshold=score_threshold)
        best_match = engine.find_best_match(query)

        seen: set[str] = set()
        candidates: list[CandidateInfo] = []

        def _make_candidate(title: str, method: str, score: float) -> CandidateInfo:
            url = f"https://en.wikisource.org/wiki/{title.replace(' ', '_')}"
            return CandidateInfo(
                title=title,
                snippet=snippet_map.get(title, ""),
                url=url,
                match_method=method,
                match_score=score,
            )

        # Best smart-search match goes first
        if best_match and best_match.matched_title not in seen:
            candidates.append(
                _make_candidate(best_match.matched_title, best_match.method, best_match.score)
            )
            seen.add(best_match.matched_title)

        # Remaining search hits, scored and sorted by fuzzy similarity
        scored_rest: list[tuple[str, float]] = []
        for title in titles:
            if title in seen:
                continue
            score = (
                _fuzz.token_sort_ratio(query, title)
                if _RAPIDFUZZ_AVAILABLE
                else 50.0
            )
            if score >= score_threshold:
                scored_rest.append((title, score))

        scored_rest.sort(key=lambda x: x[1], reverse=True)
        for title, score in scored_rest:
            if len(candidates) >= top_n:
                break
            candidates.append(_make_candidate(title, "search", score))
            seen.add(title)

        return candidates[:top_n]

    def fetch_page_by_title(self, query: str, title: str) -> PageResult:
        """
        Fetch the wikitext for a *title* explicitly chosen by the user.

        This skips the search / smart-match phase and goes straight to
        content retrieval, so it is suitable for the download phase after
        the user has confirmed their selection.
        """
        logger.info("Fetching user-selected page: '%s' (query='%s')", title, query)
        content, url = self._fetch_wikitext(title)
        if content is None:
            return PageResult(
                query=query,
                title=title,
                url=url,
                content="",
                match_method="user_selected",
                match_score=100.0,
                tried_variants=[title],
                success=False,
                error=f"Page '{title}' content could not be retrieved",
            )
        return PageResult(
            query=query,
            title=title,
            url=url,
            content=content,
            match_method="user_selected",
            match_score=100.0,
            tried_variants=[title],
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

    def _search_with_snippets(self, query: str, limit: int = WIKISOURCE_SEARCH_LIMIT) -> list[dict]:
        """
        Query the Wikisource search API and return raw hit dicts, each
        containing at minimum ``title`` and ``snippet`` keys.
        """
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srnamespace": "0",
            "srlimit": str(limit),
            "srprop": "snippet|titlesnippet",
            "format": "json",
        }
        try:
            data = self._get(WIKISOURCE_API_URL, params)
            return data.get("query", {}).get("search", [])
        except Exception as exc:
            logger.warning("_search_with_snippets failed for '%s': %s", query, exc)
            return []

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
