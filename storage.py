"""
storage.py — Local Repository Manager

Persists downloaded Wikisource content in a clean, structured layout:

    data/wikisource/
        <sanitised_title>/
            content.txt       — raw wikitext
            metadata.json     — title, URL, match method, score, …
    data/wikisource/index.db  — SQLite index for quick look-ups

Public API
----------
LocalRepository(base_dir: str | Path)
    .save(result: PageResult) -> Path
    .load(title: str) -> dict | None
    .list_entries() -> list[dict]
    .summary_dataframe() -> pd.DataFrame
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from wikisource_fetcher import PageResult

logger = logging.getLogger(__name__)

_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    query         TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT,
    match_method  TEXT,
    match_score   REAL,
    success       INTEGER,
    error         TEXT,
    saved_at      TEXT,
    file_path     TEXT
);
"""


_MAX_DIRNAME_LENGTH = 100


def _sanitise_dirname(name: str) -> str:
    """Convert an arbitrary string into a safe directory name."""
    name = name.strip()
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[\s]+", "_", name)
    return name[:_MAX_DIRNAME_LENGTH] or "unknown"


class LocalRepository:
    """
    Manages the local storage of downloaded Wikisource pages.

    Parameters
    ----------
    base_dir : str | Path
        Root directory for the local repository.
        Defaults to ``data/wikisource`` relative to the project root.
    """

    def __init__(self, base_dir: Optional[str | Path] = None):
        if base_dir is None:
            base_dir = Path(__file__).parent / "data" / "wikisource"
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._db_path = self.base_dir / "index.db"
        self._init_db()

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def save(self, result: PageResult) -> Path:
        """
        Persist a PageResult to disk and record it in the SQLite index.

        Returns the directory path where the files were written.
        """
        label = result.title if result.success and result.title else result.query
        dir_name = _sanitise_dirname(label)
        entry_dir = self.base_dir / dir_name
        entry_dir.mkdir(parents=True, exist_ok=True)

        # Write plain-text content
        content_path = entry_dir / "content.txt"
        content_path.write_text(result.content, encoding="utf-8")

        # Write JSON metadata
        metadata = {
            "query": result.query,
            "title": result.title,
            "url": result.url,
            "match_method": result.match_method,
            "match_score": result.match_score,
            "tried_variants": result.tried_variants,
            "success": result.success,
            "error": result.error,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        meta_path = entry_dir / "metadata.json"
        meta_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

        # Record in SQLite
        self._db_insert(metadata, str(entry_dir))

        logger.info("Saved '%s' to %s", label, entry_dir)
        return entry_dir

    def load(self, title: str) -> Optional[dict]:
        """
        Load the metadata + content for a given *title*.

        Returns a dict with keys ``metadata`` and ``content``, or ``None`` if
        not found.
        """
        dir_name = _sanitise_dirname(title)
        entry_dir = self.base_dir / dir_name
        meta_path = entry_dir / "metadata.json"
        content_path = entry_dir / "content.txt"

        if not meta_path.exists():
            return None

        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        content = content_path.read_text(encoding="utf-8") if content_path.exists() else ""
        return {"metadata": metadata, "content": content}

    def list_entries(self) -> list[dict]:
        """Return all index entries as a list of dicts."""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM entries ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def summary_dataframe(self) -> pd.DataFrame:
        """Return a tidy DataFrame of all stored entries (suitable for display)."""
        entries = self.list_entries()
        if not entries:
            return pd.DataFrame(columns=["query", "title", "match_method", "match_score", "success", "error", "saved_at"])
        df = pd.DataFrame(entries)
        display_cols = ["query", "title", "match_method", "match_score", "success", "error", "saved_at"]
        return df[[c for c in display_cols if c in df.columns]]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(_DB_SCHEMA)

    def _db_insert(self, metadata: dict, file_path: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO entries
                   (query, title, url, match_method, match_score, success, error, saved_at, file_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    metadata["query"],
                    metadata["title"],
                    metadata["url"],
                    metadata["match_method"],
                    metadata["match_score"],
                    int(metadata["success"]),
                    metadata["error"],
                    metadata["saved_at"],
                    file_path,
                ),
            )
