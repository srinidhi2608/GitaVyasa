"""
main_app.py — GitaVyasa Streamlit Application

Three-phase UX:
  Phase 1 — INPUT : User types comma-separated source names and clicks
                    "🔍 Find Matches".  No download happens yet.
  Phase 2 — SELECT: For every query the app shows the top Wikisource
                    candidates (title, match confidence, snippet preview).
                    Clicking a radio updates the preview panel on the right
                    so the user can inspect each result before committing.
  Phase 3 — DONE  : After the user clicks "⬇️ Download Selected" the app
                    downloads only the user-confirmed titles, shows a
                    real-time progress bar + log, then a summary table.

Run with:
    streamlit run main_app.py
"""

from __future__ import annotations

import logging
from queue import Empty, Queue

import pandas as pd
import streamlit as st

from config import LOCAL_REPO_DIR, DEFAULT_RATE_LIMIT_SECONDS
from storage import LocalRepository
from wikisource_fetcher import CandidateInfo, PageResult, WikisourceFetcher

# ---------------------------------------------------------------------------
# Page config (must be the very first Streamlit call)
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="GitaVyasa — Wikisource Extractor",
    page_icon="📜",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
    /* Log console */
    .log-box {
        background: #0e1117;
        color: #00ff88;
        font-family: monospace;
        font-size: 0.82rem;
        padding: 12px 16px;
        border-radius: 6px;
        height: 260px;
        overflow-y: auto;
        white-space: pre-wrap;
        word-break: break-word;
    }
    /* Wikisource snippet preview card */
    .snippet-card {
        background: #1a2634;
        color: #dde6f0;
        padding: 12px 16px;
        border-radius: 6px;
        border-left: 3px solid #4a9eda;
        font-size: 0.88rem;
        line-height: 1.6;
        min-height: 80px;
    }
    /* Highlight matching terms in snippet */
    .snippet-card .searchmatch {
        background: #ffd700;
        color: #111;
        padding: 0 2px;
        border-radius: 2px;
        font-weight: 700;
    }
    /* Query section header */
    .query-header {
        background: #1e2d3d;
        padding: 8px 14px;
        border-radius: 6px;
        border-left: 4px solid #4a9eda;
        margin-bottom: 6px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Logging → Queue bridge
# ---------------------------------------------------------------------------
_log_queue: Queue[str] = Queue()


class _QueueHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _log_queue.put(self.format(record))


_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)
if not any(isinstance(h, _QueueHandler) for h in _root_logger.handlers):
    _qh = _QueueHandler()
    _qh.setFormatter(logging.Formatter("%(levelname)s │ %(name)s │ %(message)s"))
    _root_logger.addHandler(_qh)

# ---------------------------------------------------------------------------
# Session-state initialisation
# ---------------------------------------------------------------------------
_STATE_DEFAULTS: dict = {
    "phase": "input",              # "input" | "select" | "done"
    "queries": [],                 # list[str]  — parsed from textarea
    "candidates_per_query": {},    # dict[str, list[CandidateInfo]]
    "user_selections": {},         # dict[str, str]  query -> chosen title
    "results": [],                 # list[PageResult]
    "log_lines": [],
    "running": False,
    "finished": False,
}
for _k, _v in _STATE_DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ---------------------------------------------------------------------------
# Sidebar — configuration
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("⚙️ Settings")
    rate_limit = st.slider(
        "Rate-limit (seconds between requests)",
        min_value=0.5,
        max_value=5.0,
        value=DEFAULT_RATE_LIMIT_SECONDS,
        step=0.5,
        help="Increase to be gentler on the Wikisource servers.",
    )
    timeout = st.slider(
        "Request timeout (seconds)",
        min_value=5,
        max_value=60,
        value=15,
        step=5,
    )
    score_threshold = st.slider(
        "Fuzzy-match minimum score",
        min_value=40,
        max_value=95,
        value=65,
        step=5,
        help="Lower = more permissive; higher = stricter.",
    )
    st.divider()
    st.markdown("**Local repository**")
    st.code(LOCAL_REPO_DIR, language=None)

# ---------------------------------------------------------------------------
# Page header
# ---------------------------------------------------------------------------
st.title("📜 GitaVyasa — Wikisource Text Extractor")
st.caption(
    "Find and preview Wikisource matches for Sanskrit / Indic texts before downloading."
)

# ===========================================================================
# PHASE 1 — INPUT
# ===========================================================================
if st.session_state.phase == "input":
    st.markdown("### 📥 Sources to find")
    raw_input = st.text_area(
        label="Enter comma-separated source names",
        placeholder="Bhagavad Gita, Mahabharata, Ramayana, Shiva Purana, Upanishads",
        height=100,
        help="Separate titles with commas. Transliteration variants are handled automatically.",
        key="raw_input",
    )

    col_find, col_clear = st.columns([1, 6])
    find_btn = col_find.button("🔍 Find Matches", type="primary")
    if col_clear.button("🗑  Clear"):
        for k, v in _STATE_DEFAULTS.items():
            st.session_state[k] = v
        st.rerun()

    if find_btn:
        queries = [q.strip() for q in raw_input.split(",") if q.strip()]
        if not queries:
            st.warning("Please enter at least one source name.")
            st.stop()

        st.session_state.queries = queries
        st.session_state.candidates_per_query = {}
        st.session_state.user_selections = {}

        fetcher = WikisourceFetcher(rate_limit_seconds=rate_limit, timeout=timeout)

        with st.spinner("Searching Wikisource for candidates — please wait…"):
            progress = st.progress(0)
            for i, query in enumerate(queries):
                progress.progress(
                    i / len(queries),
                    text=f"Searching {i + 1}/{len(queries)}: **{query}**",
                )
                candidates = fetcher.search_candidates(
                    query,
                    top_n=6,
                    score_threshold=float(score_threshold),
                )
                st.session_state.candidates_per_query[query] = candidates
                # Default selection: best match (first in ranked list)
                if candidates:
                    st.session_state.user_selections[query] = candidates[0].title
            progress.progress(1.0, text="Search complete!")

        st.session_state.phase = "select"
        st.rerun()

# ===========================================================================
# PHASE 2 — SELECT (user picks a match per query and previews it)
# ===========================================================================
elif st.session_state.phase == "select":
    st.markdown("### 🎯 Review and select matches")
    st.info(
        "For each source, choose the best Wikisource match using the radio buttons. "
        "The preview panel on the right updates instantly so you can inspect each "
        "option before confirming. When ready, click **⬇️ Download Selected**."
    )

    _BADGE = {
        "exact":      "🟢",
        "alias":      "🔵",
        "normalised": "🟣",
        "phonetic":   "🟠",
        "fuzzy":      "🟡",
        "search":     "⚪",
    }
    _METHOD_LABEL = {
        "exact":      "Exact match",
        "alias":      "Known alias",
        "normalised": "Normalised spelling",
        "phonetic":   "Phonetic match",
        "fuzzy":      "Fuzzy match",
        "search":     "Search result",
    }

    for i, query in enumerate(st.session_state.queries):
        candidates: list[CandidateInfo] = st.session_state.candidates_per_query.get(query, [])

        st.markdown(
            f"<div class='query-header'>🔍 <strong>{query}</strong></div>",
            unsafe_allow_html=True,
        )

        if not candidates:
            st.warning(f"No Wikisource matches found for **{query}**. It will be skipped.")
            st.divider()
            continue

        # Build option labels: "🟢 Bhagavad Gita  (exact · 100%)"
        score_map = {c.title: c for c in candidates}

        def _fmt(title: str) -> str:
            c = score_map[title]
            badge = _BADGE.get(c.match_method, "⚪")
            method = _METHOD_LABEL.get(c.match_method, c.match_method)
            return f"{badge} {title}   ·  {method}  ·  {c.match_score:.0f}%"

        col_radio, col_preview = st.columns([5, 7])

        with col_radio:
            radio_key = f"radio_{i}"
            selected_title = st.radio(
                "Available matches:",
                options=[c.title for c in candidates],
                format_func=_fmt,
                key=radio_key,
                label_visibility="visible",
            )
            # Persist the user's current selection
            st.session_state.user_selections[query] = selected_title

        with col_preview:
            sel = score_map.get(selected_title)
            if sel:
                st.markdown(f"**📄 {sel.title}**")
                st.markdown(
                    f"<div class='snippet-card'>"
                    f"{sel.snippet if sel.snippet else '<em>No preview snippet available.</em>'}"
                    f"</div>",
                    unsafe_allow_html=True,
                )
                st.markdown(
                    f"🔗 [Open this page in Wikisource ↗]({sel.url})",
                )
                badge = _BADGE.get(sel.match_method, "⚪")
                method = _METHOD_LABEL.get(sel.match_method, sel.match_method)
                st.caption(f"{badge} Match: **{method}** &nbsp;|&nbsp; Confidence: **{sel.match_score:.0f}%**")

        st.divider()

    col_dl, col_back, _ = st.columns([2, 2, 8])
    download_btn = col_dl.button("⬇️ Download Selected", type="primary")
    if col_back.button("← Back to Input"):
        st.session_state.phase = "input"
        st.rerun()

    # ------------------------------------------------------------------
    # Download phase (triggered from within the select phase rendering)
    # ------------------------------------------------------------------
    if download_btn:
        selections = [
            (q, st.session_state.user_selections.get(q))
            for q in st.session_state.queries
            if st.session_state.user_selections.get(q)
        ]

        if not selections:
            st.warning("No selections to download.")
            st.stop()

        st.session_state.running = True
        st.session_state.results = []
        st.session_state.log_lines = []

        fetcher = WikisourceFetcher(rate_limit_seconds=rate_limit, timeout=timeout)
        repo = LocalRepository(LOCAL_REPO_DIR)
        total = len(selections)
        log_lines: list[str] = []

        def _flush_logs() -> None:
            while True:
                try:
                    log_lines.append(_log_queue.get_nowait())
                except Empty:
                    break

        progress_bar = st.progress(0, text="Starting download…")
        log_box = st.empty()

        for idx, (query, title) in enumerate(selections, start=1):
            progress_bar.progress(
                (idx - 1) / total,
                text=f"Downloading {idx}/{total}: **{title}**",
            )
            result = fetcher.fetch_page_by_title(query, title)
            repo.save(result)
            st.session_state.results.append(result)

            _flush_logs()

            if result.success:
                log_lines.append(
                    f"✅ [{idx}/{total}] '{title}' downloaded successfully"
                )
            else:
                log_lines.append(
                    f"❌ [{idx}/{total}] '{title}' FAILED — {result.error}"
                )

            st.session_state.log_lines = log_lines.copy()
            log_box.markdown(
                "<div class='log-box'>" + "<br>".join(log_lines[-40:]) + "</div>",
                unsafe_allow_html=True,
            )

        _flush_logs()
        progress_bar.progress(1.0, text=f"Done — {total} source(s) downloaded.")
        st.session_state.running = False
        st.session_state.finished = True
        st.session_state.phase = "done"
        st.rerun()

# ===========================================================================
# PHASE 3 — DONE (results summary)
# ===========================================================================
elif st.session_state.phase == "done":
    results: list[PageResult] = st.session_state.results

    if st.session_state.log_lines:
        st.markdown("#### 📋 Download log")
        st.markdown(
            "<div class='log-box'>"
            + "<br>".join(st.session_state.log_lines[-40:])
            + "</div>",
            unsafe_allow_html=True,
        )
        st.divider()

    if results:
        st.subheader("📊 Results Summary")
        rows = []
        for r in results:
            rows.append(
                {
                    "Status": "✅ Success" if r.success else "❌ Failed",
                    "Query": r.query,
                    "Downloaded Title": r.title or "—",
                    "Error": r.error or "—",
                }
            )
        st.dataframe(pd.DataFrame(rows), use_container_width=True)

        successes = sum(1 for r in results if r.success)
        failures = len(results) - successes
        m1, m2, m3 = st.columns(3)
        m1.metric("Total", len(results))
        m2.metric("✅ Succeeded", successes)
        m3.metric("❌ Failed", failures)

        repo = LocalRepository(LOCAL_REPO_DIR)
        csv_bytes = repo.summary_dataframe().to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Download full index as CSV",
            data=csv_bytes,
            file_name="wikisource_index.csv",
            mime="text/csv",
        )

    col_new, _ = st.columns([2, 10])
    if col_new.button("🔄 Start New Search", type="primary"):
        for k, v in _STATE_DEFAULTS.items():
            st.session_state[k] = v
        st.rerun()

