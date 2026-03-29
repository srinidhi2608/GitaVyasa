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
  4. Parse every downloaded page's wikitext for internal ``[[...]]`` links
     that point to sub-pages (titles containing ``/``).  Download those
     sub-pages too — even if they are not tagged with the category — and
     mirror the ``/`` hierarchy as nested directories under the parent.
     This handles index/TOC pages whose sections (e.g. प्रश्नः १, २, ३)
     are linked inside the wikitext but never added to the category.
  5. Recurse into every sub-category (up to *max_depth* levels), creating
     nested sub-directories that mirror the category hierarchy.
  6. Write an ``index.json`` summary at the category root directory.
  7. Write a timestamped per-run log file under
         <output_dir>/logs/run_<YYYYMMDDTHHMMSS>_<category>.log
     recording SUCCESS / FAILURE for every page, including the exact URL
     and saved file path on success, or the error message on failure.
  8. After the full crawl, create a concatenated ``<dirname>.txt`` file
     adjacent to every directory so each category/sub-category can be
     read as a single text file.  Processing is bottom-up so a parent's
     concat file automatically includes the children's already-merged
     content.

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
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, IO, Optional
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

# Width of the ``=`` separator banners used in concatenated output files.
_CONCAT_BANNER_WIDTH = 60

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

# ---------------------------------------------------------------------------
# Wikitext link extraction
# ---------------------------------------------------------------------------
# Matches [[target]] and [[target|display text]] wiki links.
_WIKI_LINK_RE = re.compile(r"\[\[([^\[\]|]+?)(?:\|[^\[\]]*)?\]\]")

# Namespace prefixes (Sanskrit and English) that signal a non-content page.
# Links whose title begins with one of these followed by ":" are skipped.
_SKIP_NS_PREFIXES: frozenset[str] = frozenset([
    # Sanskrit Wikisource namespaces
    "वर्गः",       # Category
    "वार्ता",       # Talk
    "सदस्यः",      # User
    "चित्रम्",      # File / Image
    "साहाय्यम्",   # Help
    "विकिस्रोतः",  # Wikisource project
    # English equivalents used on multilingual installations
    "Category", "Talk", "User", "File", "Image", "Help",
    "Wikisource", "Wikipedia", "MediaWiki", "Template", "Module", "Portal",
    "User talk", "File talk", "Template talk", "Category talk",
])


def _sanitise_filename(name: str) -> str:
    """Convert *name* into a safe file or directory name on any OS.

    Devanagari characters are preserved; characters that are illegal in
    file names (colon, slash, backslash, …) are replaced with ``_``.
    """
    name = name.strip()
    name = _UNSAFE_FILENAME_RE.sub("_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name[:_MAX_FILENAME_LEN] or "unknown"


def _extract_wikitext_links(wikitext: str, base_title: str = "") -> list[str]:
    """Extract navigable internal page links from *wikitext*.

    Returns a deduplicated list of page titles to follow, in order of first
    appearance.  Category links, file embeds, and section anchors are
    excluded.

    If *base_title* is supplied, MediaWiki relative sub-page links that start
    with ``/`` are resolved against the root of *base_title*:

        ``/प्रश्नः १``  +  ``आग्निवेश्यगृह्यसूत्रम्``
        →  ``आग्निवेश्यगृह्यसूत्रम्/प्रश्नः १``
    """
    seen: set[str] = set()
    titles: list[str] = []
    # Root of the current page title (part before the first "/")
    base_root = base_title.split("/")[0] if base_title else ""

    for m in _WIKI_LINK_RE.finditer(wikitext):
        raw = m.group(1).strip()

        # Skip pure section anchors
        if raw.startswith("#"):
            continue

        # Resolve relative sub-page links (e.g. /प्रश्नः १ → Parent/प्रश्नः १)
        if raw.startswith("/"):
            if base_root:
                raw = base_root + raw
            else:
                continue

        # Strip explicit main-namespace prefix (":Page" → "Page")
        if raw.startswith(":"):
            raw = raw[1:].strip()

        # Skip known non-content namespaces
        colon_pos = raw.find(":")
        if colon_pos > 0 and raw[:colon_pos].strip() in _SKIP_NS_PREFIXES:
            continue

        if not raw or raw == base_title:
            continue

        if raw not in seen:
            seen.add(raw)
            titles.append(raw)

    return titles


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
    log_path: str = ""

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
        # Set to an open file handle during run(); None otherwise.
        self._run_log: Optional[IO[str]] = None

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

        # -----------------------------------------------------------------
        # Open a timestamped per-run log file.
        # -----------------------------------------------------------------
        log_dir = self.output_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        run_ts = datetime.now(timezone.utc)
        log_filename = (
            f"run_{run_ts.strftime('%Y%m%d_%H%M%S')}"
            f"_{_sanitise_filename(category_name)}.log"
        )
        log_path = log_dir / log_filename
        log_file = log_path.open("w", encoding="utf-8")
        self._run_log = log_file
        log_file.write(
            f"Run started : {run_ts.isoformat()}\n"
            f"Category    : {category_url}\n"
            f"Output dir  : {self.output_dir}\n"
            f"{'-' * 72}\n\n"
        )
        log_file.flush()

        visited_categories: set[str] = set()
        visited_pages: set[str] = set()
        try:
            self._crawl_category(
                category_title=category_title,
                current_dir=root_dir,
                depth=0,
                result=result,
                visited=visited_categories,
                visited_pages=visited_pages,
                progress_callback=progress_callback,
            )
        finally:
            end_ts = datetime.now(timezone.utc)
            log_file.write(
                f"\n{'-' * 72}\n"
                f"Run ended   : {end_ts.isoformat()}\n"
                f"Total       : {result.total_count}\n"
                f"Succeeded   : {result.success_count}\n"
                f"Failed      : {result.failure_count}\n"
            )
            log_file.close()
            self._run_log = None

        logger.info(
            "Batch complete: %d downloaded, %d failed (total %d)",
            result.success_count,
            result.failure_count,
            result.total_count,
        )
        result.log_path = str(log_path)
        logger.info("Run log written to %s", log_path)
        self._write_index(result, root_dir)
        self._write_concat_files(root_dir)
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
        visited_pages: set[str],
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
                    visited_pages=visited_pages,
                    progress_callback=progress_callback,
                )
            else:
                if member.title in visited_pages:
                    logger.debug("Skipping already-visited page '%s'", member.title)
                    continue
                visited_pages.add(member.title)
                downloaded = self._fetch_and_save(member, current_dir)
                result.pages.append(downloaded)
                self._log_page_result(downloaded)
                if progress_callback:
                    progress_callback(downloaded)
                # Follow any sub-page links embedded in this page's wikitext.
                if downloaded.success:
                    self._crawl_page_links(
                        parent_title=member.title,
                        wikitext=downloaded.content,
                        output_dir=current_dir,
                        depth=depth + 1,
                        result=result,
                        visited_pages=visited_pages,
                        progress_callback=progress_callback,
                    )

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

    def _resolve_output_path(self, title: str, base_dir: Path) -> Path:
        """Map a page *title* to a filesystem path under *base_dir*.

        MediaWiki sub-page separators (``/``) are reflected as nested
        directories so that the on-disk layout mirrors the page hierarchy:

        .. code-block:: text

            "A"         →  base_dir/A.txt
            "A/B"       →  base_dir/A/B.txt
            "A/B/C"     →  base_dir/A/B/C.txt
        """
        parts = [_sanitise_filename(p) for p in title.split("/") if p.strip()]
        if not parts:
            return base_dir / "unknown.txt"
        if len(parts) == 1:
            return base_dir / (parts[0] + ".txt")
        return base_dir.joinpath(*parts[:-1]) / (parts[-1] + ".txt")

    def _crawl_page_links(
        self,
        parent_title: str,
        wikitext: str,
        output_dir: Path,
        depth: int,
        result: BatchResult,
        visited_pages: set[str],
        progress_callback: Optional[Callable[[DownloadedPage], None]],
    ) -> None:
        """Parse *wikitext* for ``[[...]]`` links and download any sub-pages.

        Only links whose titles contain ``/`` are followed — these are
        MediaWiki sub-pages (e.g. ``आग्निवेश्यगृह्यसूत्रम्/प्रश्नः २``)
        that act as sub-sections of the parent page.  Pages already in
        *visited_pages* are skipped, preventing duplicate downloads.

        Sub-pages are saved under *output_dir* using
        :meth:`_resolve_output_path` so the ``/`` hierarchy is reflected
        as nested directories.
        """
        if depth > self.max_depth:
            logger.warning(
                "Max recursion depth %d reached while following links from '%s' — stopping",
                self.max_depth,
                parent_title,
            )
            return

        all_links = _extract_wikitext_links(wikitext, base_title=parent_title)
        # Pre-filter: skip non-sub-page links and pages already known to be
        # visited.  The inner check at the top of the loop (line below) is the
        # authoritative guard; this pre-filter just avoids building an inflated
        # log count and iterating links that were already visited before this
        # call started.
        new_links = [t for t in all_links if "/" in t and t not in visited_pages]

        if not new_links:
            return

        logger.info(
            "[link-follow depth=%d] '%s': %d sub-page link(s)",
            depth, parent_title, len(new_links),
        )

        for linked_title in new_links:
            if linked_title in visited_pages:
                continue
            visited_pages.add(linked_title)

            output_path = self._resolve_output_path(linked_title, output_dir)
            logger.info("[link-follow] Fetching: '%s'", linked_title)

            content, linked_url = self.fetch_page_text(linked_title)

            if content is not None:
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(content, encoding="utf-8")
                logger.info("[link-follow] Saved '%s' → %s", linked_title, output_path)
                downloaded = DownloadedPage(
                    title=linked_title,
                    url=linked_url,
                    content=content,
                    output_path=str(output_path),
                    success=True,
                )
            else:
                downloaded = DownloadedPage(
                    title=linked_title,
                    url=linked_url,
                    content="",
                    output_path=str(output_path),
                    success=False,
                    error=f"Content could not be retrieved for '{linked_title}'",
                )

            result.pages.append(downloaded)
            self._log_page_result(downloaded)
            if progress_callback:
                progress_callback(downloaded)

            # Recurse into this page's own sub-links
            if downloaded.success:
                self._crawl_page_links(
                    parent_title=linked_title,
                    wikitext=downloaded.content,
                    output_dir=output_dir,
                    depth=depth + 1,
                    result=result,
                    visited_pages=visited_pages,
                    progress_callback=progress_callback,
                )

    def _log_page_result(self, page: DownloadedPage) -> None:
        """Append one SUCCESS or FAILURE line to the active run log file.

        Does nothing when called outside of a :meth:`run` invocation (i.e.
        when :attr:`_run_log` is ``None``).
        """
        if self._run_log is None:
            return
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if page.success:
            line = (
                f"[{ts}] SUCCESS"
                f" | {page.title}"
                f" | URL: {page.url}"
                f" | File: {page.output_path}\n"
            )
        else:
            line = (
                f"[{ts}] FAILURE"
                f" | {page.title}"
                f" | URL: {page.url}"
                f" | Error: {page.error}\n"
            )
        self._run_log.write(line)
        self._run_log.flush()

    def _write_concat_files(self, root_dir: Path) -> None:
        """Create a ``<dirname>.txt`` file adjacent to a directory **only**
        when that directory contains exclusively ``.txt`` files (no
        sub-directories).  Directories that contain sub-directories are
        skipped — the intent is to keep each book's individual section files
        together in one place without creating spurious roll-ups at higher
        levels of the hierarchy.

        Example::

            गृह्यसूत्रम्/
              आपस्तम्बगृह्यसूत्रम्.txt     ← direct .txt file, no sub-dirs
              आग्निवेश्यगृह्यसूत्रम्/       ← sub-directory with sections
                प्रश्नः_१.txt
                प्रश्नः_२.txt

        Only ``आग्निवेश्यगृह्यसूत्रम्/`` qualifies (no sub-dirs inside it).
        The result is ``गृह्यसूत्रम्/आग्निवेश्यगृह्यसूत्रम्.txt``.
        ``गृह्यसूत्रम्/`` itself is skipped because it has a sub-directory.

        ``index.json`` and other non-``.txt`` files are ignored.
        """
        concat_count = 0
        for dirpath, dirs, files in os.walk(str(root_dir)):
            # Skip directories that contain sub-directories.
            if dirs:
                continue
            dp = Path(dirpath)
            txt_files = sorted(dp / f for f in files if f.endswith(".txt"))
            if not txt_files:
                continue
            target = dp.parent / (dp.name + ".txt")
            chunks: list[str] = []
            for txt_path in txt_files:
                header = (
                    f"{'=' * _CONCAT_BANNER_WIDTH}\n"
                    f"{txt_path.stem}\n"
                    f"{'=' * _CONCAT_BANNER_WIDTH}"
                )
                chunks.append(f"{header}\n{txt_path.read_text(encoding='utf-8')}")
            target.write_text("\n\n".join(chunks), encoding="utf-8")
            logger.info(
                "Concat → %s (%d source file(s))", target, len(txt_files)
            )
            concat_count += 1
        logger.info("Concat files written: %d", concat_count)

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
    print(f"📋  Run log    : {result.log_path}")


if __name__ == "__main__":
    main()
