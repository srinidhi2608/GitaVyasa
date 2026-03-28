"""
sa_wikisource_batch.py — Sanskrit Wikisource Batch Category Extractor

Recursively downloads every text page found under a Sanskrit Wikisource
category URL (sa.wikisource.org) and saves each page's raw wikitext as a
plain .txt file on disk.

Given a category URL such as:
    https://sa.wikisource.org/wiki/वर्गः:गृह्यसूत्रम्

the crawler will:
  1. Parse the category title from the URL.
  2. Call the MediaWiki ``categorymembers`` API to list every content page
     (namespace 0) and every sub-category in the category, handling
     pagination automatically.
  3. Fetch the raw wikitext for each content page and write it to:
         <output_dir>/<category_name>/<sanitised_page_title>.txt
  4. Recurse into every sub-category (up to *max_depth* levels), creating
     nested sub-directories that mirror the category hierarchy.
  5. Write an ``index.json`` summary at the category root directory.

Usage
-----
    # Command line:
    python sa_wikisource_batch.py \\
        "https://sa.wikisource.org/wiki/वर्गः:गृह्यसूत्रम्"

    python sa_wikisource_batch.py \\
        "https://sa.wikisource.org/wiki/वर्गः:गृह्यसूत्रम्" \\
        --output-dir data/sa_wikisource \\
        --rate-limit 1.5 \\
        --max-depth 5 \\
        --verbose

    # As a library:
    from sa_wikisource_batch import SanskritWikisourceBatch

    batch = SanskritWikisourceBatch(output_dir="data/sa_wikisource")
    result = batch.run("https://sa.wikisource.org/wiki/वर्गः:गृह्यसूत्रम्")
    print(f"Downloaded {result.success_count} pages, {result.failure_count} failed")

Public API
----------
SanskritWikisourceBatch(output_dir, rate_limit_seconds, timeout, max_depth)
    .run(category_url, progress_callback) -> BatchResult
    .list_category_pages(category_title) -> list[PageMember]
    .fetch_page_text(title) -> tuple[str | None, str]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import unquote, urlparse

import requests

from config import (
    SA_WIKISOURCE_API_URL,
    SA_WIKISOURCE_BASE_URL,
    SA_WIKISOURCE_CATEGORY_LIMIT,
    SA_LOCAL_REPO_DIR,
    DEFAULT_RATE_LIMIT_SECONDS,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 15
_DEFAULT_MAX_DEPTH = 10

# Shared HTTP session — User-Agent is required by Wikimedia's API policy.
_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": (
        "GitaVyasa/1.0 (https://github.com/srinidhi2608/GitaVyasa; "
        "educational-use) python-requests"
    )
})

# ---------------------------------------------------------------------------
# File-name sanitisation
# ---------------------------------------------------------------------------
# Keep Devanagari (U+0900–U+097F), ASCII word characters, and hyphens.
# Everything else (ASCII colon, slash, etc.) becomes an underscore.
_UNSAFE_FILENAME_RE = re.compile(r"[^\-\w\u0900-\u097F]")
_MAX_FILENAME_LEN = 200


def _sanitise_filename(name: str) -> str:
    """Convert *name* into a safe file or directory name on any OS.

    Devanagari characters are preserved; characters that are illegal in
    file names (colon, slash, backslash, …) are replaced with ``_``.
    """
    name = name.strip()
    name = _UNSAFE_FILENAME_RE.sub("_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name[:_MAX_FILENAME_LEN] or "unknown"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PageMember:
    """A single member returned by the ``categorymembers`` API."""
    title: str
    page_id: int
    member_type: str   # "page" | "subcat"
    url: str


@dataclass
class DownloadedPage:
    """Outcome of fetching and saving one wikitext page."""
    title: str
    url: str
    content: str
    output_path: str   # absolute path to the saved .txt file
    success: bool = True
    error: str = ""


@dataclass
class BatchResult:
    """Aggregate result of a full category crawl."""
    category_url: str
    output_dir: str
    pages: list[DownloadedPage] = field(default_factory=list)

    @property
    def success_count(self) -> int:
        return sum(1 for p in self.pages if p.success)

    @property
    def failure_count(self) -> int:
        return sum(1 for p in self.pages if not p.success)

    @property
    def total_count(self) -> int:
        return len(self.pages)


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class SanskritWikisourceBatch:
    """
    Recursively downloads all text pages from a Sanskrit Wikisource category.

    Parameters
    ----------
    output_dir : str | Path
        Root directory where downloaded ``.txt`` files are saved.
        Defaults to ``data/sa_wikisource`` relative to the project root.
    rate_limit_seconds : float
        Minimum seconds to wait between consecutive API calls.
        Respects Wikimedia's rate-limiting guidelines.
    timeout : int
        HTTP request timeout in seconds.
    max_depth : int
        Maximum sub-category recursion depth.  Guards against cycles and
        runaway crawls on very deeply nested category trees.
    """

    def __init__(
        self,
        output_dir: str | Path = SA_LOCAL_REPO_DIR,
        rate_limit_seconds: float = DEFAULT_RATE_LIMIT_SECONDS,
        timeout: int = _DEFAULT_TIMEOUT,
        max_depth: int = _DEFAULT_MAX_DEPTH,
    ):
        self.output_dir = Path(output_dir)
        self.rate_limit = rate_limit_seconds
        self.timeout = timeout
        self.max_depth = max_depth
        self._last_request_time: float = 0.0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(
        self,
        category_url: str,
        progress_callback: Optional[Callable[[DownloadedPage], None]] = None,
    ) -> BatchResult:
        """
        Crawl the Sanskrit Wikisource category at *category_url* and save
        every page's wikitext as a ``.txt`` file.

        The output mirrors the category hierarchy:

        .. code-block:: text

            <output_dir>/
              <category_name>/
                <page_title>.txt
                <sub_category_name>/
                  <page_title>.txt
                  ...
              index.json

        Parameters
        ----------
        category_url : str
            Full URL of the Sanskrit Wikisource category page.
            Example: ``https://sa.wikisource.org/wiki/वर्गः:गृह्यसूत्रम्``
        progress_callback : callable, optional
            Called after each page is processed as
            ``callback(DownloadedPage)``.  Useful for live progress
            reporting in a UI or script.

        Returns
        -------
        BatchResult
            Aggregate outcome including per-page download details.
        """
        category_title = _parse_category_title(category_url)
        logger.info("Starting batch download for category: '%s'", category_title)

        result = BatchResult(category_url=category_url, output_dir=str(self.output_dir))

        # Top-level output sub-directory is named after the category leaf
        # (strips namespace prefix "वर्गः:" / "Category:" etc.)
        category_name = category_title.split(":", 1)[-1]
        root_dir = self.output_dir / _sanitise_filename(category_name)

        visited_categories: set[str] = set()
        self._crawl_category(
            category_title=category_title,
            current_dir=root_dir,
            depth=0,
            result=result,
            visited=visited_categories,
            progress_callback=progress_callback,
        )

        logger.info(
            "Batch complete: %d downloaded, %d failed (total %d)",
            result.success_count,
            result.failure_count,
            result.total_count,
        )
        self._write_index(result, root_dir)
        return result

    def list_category_pages(self, category_title: str) -> list[PageMember]:
        """
        Return all members (content pages and sub-categories) of
        *category_title*, fetching all pages of the API response.

        Parameters
        ----------
        category_title : str
            Category page title including the Sanskrit namespace prefix,
            e.g. ``वर्गः:गृह्यसूत्रम्``.

        Returns
        -------
        list[PageMember]
            All pages (``member_type="page"``) and sub-categories
            (``member_type="subcat"``) found in the category.
        """
        members: list[PageMember] = []
        continue_token: Optional[str] = None

        while True:
            params: dict = {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": category_title,
                "cmtype": "page|subcat",
                "cmlimit": str(SA_WIKISOURCE_CATEGORY_LIMIT),
                "cmprop": "ids|title|type",
                "format": "json",
                "formatversion": "2",
            }
            if continue_token:
                params["cmcontinue"] = continue_token

            try:
                data = self._get(SA_WIKISOURCE_API_URL, params)
            except Exception as exc:
                logger.error("Failed to list category '%s': %s", category_title, exc)
                break

            for item in data.get("query", {}).get("categorymembers", []):
                member_type = "subcat" if item.get("type") == "subcat" else "page"
                title = item.get("title", "")
                url = SA_WIKISOURCE_BASE_URL + title.replace(" ", "_")
                members.append(
                    PageMember(
                        title=title,
                        page_id=item.get("pageid", 0),
                        member_type=member_type,
                        url=url,
                    )
                )

            continue_token = data.get("continue", {}).get("cmcontinue")
            if not continue_token:
                break

        logger.debug("Category '%s': found %d members", category_title, len(members))
        return members

    def fetch_page_text(self, title: str) -> tuple[Optional[str], str]:
        """
        Retrieve the raw wikitext of a Sanskrit Wikisource page.

        Parameters
        ----------
        title : str
            Exact page title as returned by the ``categorymembers`` API.

        Returns
        -------
        tuple[str | None, str]
            ``(wikitext_content, canonical_url)`` — *content* is ``None``
            on any retrieval error (missing page, timeout, network error).
        """
        url = SA_WIKISOURCE_BASE_URL + title.replace(" ", "_")
        params = {
            "action": "query",
            "titles": title,
            "prop": "revisions",
            "rvprop": "content",
            "rvslots": "main",
            "format": "json",
            "formatversion": "2",
        }
        try:
            data = self._get(SA_WIKISOURCE_API_URL, params)
            pages = data.get("query", {}).get("pages", [])
            for page_data in pages:
                if page_data.get("missing"):
                    logger.warning("Page '%s' is marked missing on sa.wikisource.org", title)
                    return None, url
                revisions = page_data.get("revisions", [])
                if revisions:
                    content = (
                        revisions[0]
                        .get("slots", {})
                        .get("main", {})
                        .get("content", "")
                    )
                    if content:
                        return content, url
            return None, url
        except requests.exceptions.Timeout:
            logger.error("Timeout fetching '%s'", title)
            return None, url
        except requests.exceptions.RequestException as exc:
            logger.error("Request error for '%s': %s", title, exc)
            return None, url

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _crawl_category(
        self,
        category_title: str,
        current_dir: Path,
        depth: int,
        result: BatchResult,
        visited: set[str],
        progress_callback: Optional[Callable[[DownloadedPage], None]],
    ) -> None:
        """Recursively crawl *category_title* and save all content pages."""
        if depth > self.max_depth:
            logger.warning(
                "Max recursion depth %d reached at '%s' — skipping",
                self.max_depth,
                category_title,
            )
            return

        if category_title in visited:
            logger.debug("Skipping already-visited category '%s'", category_title)
            return
        visited.add(category_title)

        current_dir.mkdir(parents=True, exist_ok=True)

        members = self.list_category_pages(category_title)
        logger.info(
            "[depth=%d] '%s': %d member(s)", depth, category_title, len(members)
        )

        for member in members:
            if member.member_type == "subcat":
                # Mirror the sub-category as a sub-directory.
                # Strip the namespace prefix (e.g. "वर्गः:") from the dir name.
                sub_cat_name = member.title.split(":", 1)[-1]
                sub_dir = current_dir / _sanitise_filename(sub_cat_name)
                self._crawl_category(
                    category_title=member.title,
                    current_dir=sub_dir,
                    depth=depth + 1,
                    result=result,
                    visited=visited,
                    progress_callback=progress_callback,
                )
            else:
                downloaded = self._fetch_and_save(member, current_dir)
                result.pages.append(downloaded)
                if progress_callback:
                    progress_callback(downloaded)

    def _fetch_and_save(self, member: PageMember, output_dir: Path) -> DownloadedPage:
        """Fetch *member*'s wikitext and write it to *output_dir*."""
        title = member.title
        logger.info("Fetching page: '%s'", title)

        content, url = self.fetch_page_text(title)

        # Build a safe filename: replace path-separator chars with underscores,
        # keep Devanagari characters intact.
        filename = _sanitise_filename(title) + ".txt"
        output_path = output_dir / filename

        if content is None:
            return DownloadedPage(
                title=title,
                url=url,
                content="",
                output_path=str(output_path),
                success=False,
                error=f"Content could not be retrieved for '{title}'",
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        logger.info("Saved '%s' → %s", title, output_path)

        return DownloadedPage(
            title=title,
            url=url,
            content=content,
            output_path=str(output_path),
            success=True,
        )

    def _write_index(self, result: BatchResult, root_dir: Path) -> None:
        """Write an ``index.json`` summary of all download attempts to *root_dir*."""
        root_dir.mkdir(parents=True, exist_ok=True)
        index_path = root_dir / "index.json"
        now = datetime.now(timezone.utc).isoformat()
        index = {
            "category_url": result.category_url,
            "output_dir": result.output_dir,
            "total": result.total_count,
            "success": result.success_count,
            "failure": result.failure_count,
            "saved_at": now,
            "pages": [
                {
                    "title": p.title,
                    "url": p.url,
                    "output_path": p.output_path,
                    "success": p.success,
                    "error": p.error,
                }
                for p in result.pages
            ],
        }
        index_path.write_text(
            json.dumps(index, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Index written to %s", index_path)

    def _throttle(self) -> None:
        """Enforce the configured rate limit between consecutive API calls."""
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


# ---------------------------------------------------------------------------
# Module-level helper (also used by tests)
# ---------------------------------------------------------------------------

def _parse_category_title(url: str) -> str:
    """
    Extract the category page title from a full Sanskrit Wikisource URL.

    ``https://sa.wikisource.org/wiki/वर्गः:गृह्यसूत्रम्``
    → ``वर्गः:गृह्यसूत्रम्``

    Handles both URL-encoded (``%E0%A4%B5…``) and plain Unicode URLs.
    """
    parsed = urlparse(url)
    path = parsed.path
    if path.startswith("/wiki/"):
        path = path[len("/wiki/"):]
    # URL-decode percent-encoded characters then normalise spaces
    return unquote(path).replace("_", " ")


# ---------------------------------------------------------------------------
# Command-line entry point
# ---------------------------------------------------------------------------

def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s │ %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Batch-download all pages from a Sanskrit Wikisource category.\n\n"
            "Example:\n"
            "  python sa_wikisource_batch.py \\\n"
            '    "https://sa.wikisource.org/wiki/वर्गः:गृह्यसूत्रम्"'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "category_url",
        help=(
            "Full URL of a Sanskrit Wikisource category page.  "
            "Example: https://sa.wikisource.org/wiki/वर्गः:गृह्यसूत्रम्"
        ),
    )
    parser.add_argument(
        "--output-dir",
        default=SA_LOCAL_REPO_DIR,
        help=f"Root directory for downloaded .txt files (default: {SA_LOCAL_REPO_DIR})",
    )
    parser.add_argument(
        "--rate-limit",
        type=float,
        default=DEFAULT_RATE_LIMIT_SECONDS,
        help=f"Minimum seconds between API calls (default: {DEFAULT_RATE_LIMIT_SECONDS})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=_DEFAULT_TIMEOUT,
        help=f"HTTP timeout in seconds (default: {_DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=_DEFAULT_MAX_DEPTH,
        help=f"Maximum sub-category recursion depth (default: {_DEFAULT_MAX_DEPTH})",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable DEBUG-level logging",
    )
    args = parser.parse_args()
    _setup_logging(args.verbose)

    batch = SanskritWikisourceBatch(
        output_dir=args.output_dir,
        rate_limit_seconds=args.rate_limit,
        timeout=args.timeout,
        max_depth=args.max_depth,
    )

    def _print_progress(page: DownloadedPage) -> None:
        status = "✅" if page.success else "❌"
        print(f"  {status}  {page.title}")
        if not page.success:
            print(f"      Error: {page.error}")

    print(f"📥  Starting download from: {args.category_url}")
    print(f"📂  Output directory:       {args.output_dir}\n")

    result = batch.run(args.category_url, progress_callback=_print_progress)

    print(f"\n{'=' * 60}")
    print(f"✅  Downloaded : {result.success_count}")
    print(f"❌  Failed     : {result.failure_count}")
    print(f"📄  Total      : {result.total_count}")
    print(f"📂  Saved to   : {result.output_dir}")


if __name__ == "__main__":
    main()
