"""
main_app.py — GitaVyasa Streamlit Application

Provides a modern, user-friendly UI for:
  1. Bulk extraction of texts from Wikisource (comma-separated source names)
  2. Real-time progress bar and live log panel
  3. Smart-search fallback logging (alternate spellings tried)
  4. Success/failure summary table
  5. Download button for the local repository index

Run with:
    streamlit run main_app.py
"""

from __future__ import annotations

import logging
import threading
import time
from queue import Empty, Queue

import pandas as pd
import streamlit as st

from config import LOCAL_REPO_DIR, DEFAULT_RATE_LIMIT_SECONDS
from storage import LocalRepository
from wikisource_fetcher import PageResult, WikisourceFetcher

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
# Minimal custom CSS for a cleaner look
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
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
    .success-badge  { color: #00c853; font-weight: 700; }
    .failure-badge  { color: #ff1744; font-weight: 700; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ---------------------------------------------------------------------------
# Logging → Queue bridge (so backend logs appear in the UI)
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
def _init_state() -> None:
    defaults = {
        "results": [],
        "log_lines": [],
        "running": False,
        "finished": False,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


_init_state()

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
        help="Lower = more permissive matching; higher = stricter.",
    )
    st.divider()
    st.markdown("**Local repository**")
    st.code(LOCAL_REPO_DIR, language=None)

# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------
st.title("📜 GitaVyasa — Wikisource Text Extractor")
st.caption(
    "Extract Sanskrit / Indic texts from Wikisource with smart transliteration-variant matching."
)

st.markdown("### 📥 Sources to download")
raw_input = st.text_area(
    label="Enter comma-separated source names",
    placeholder="Bhagavad Gita, Mahabharata, Ramayana, Shiva Purana, Upanishads",
    height=100,
    help="Separate multiple titles with commas. Transliteration variants are handled automatically.",
)

col_run, col_clear = st.columns([1, 6])
run_button = col_run.button("▶  Extract", type="primary", disabled=st.session_state.running)
if col_clear.button("🗑  Clear results"):
    st.session_state.results = []
    st.session_state.log_lines = []
    st.session_state.finished = False
    st.rerun()

st.divider()

# ---------------------------------------------------------------------------
# Progress + log area (always visible)
# ---------------------------------------------------------------------------
progress_placeholder = st.empty()
log_placeholder = st.empty()

# ---------------------------------------------------------------------------
# Results table (shown after run)
# ---------------------------------------------------------------------------
results_placeholder = st.empty()


def _render_results(results: list[PageResult]) -> None:
    if not results:
        return
    rows = []
    for r in results:
        status = "✅ Success" if r.success else "❌ Failed"
        tried = " → ".join(r.tried_variants) if r.tried_variants else r.query
        rows.append(
            {
                "Status": status,
                "Query": r.query,
                "Matched Title": r.title or "—",
                "Method": r.match_method,
                "Score": f"{r.match_score:.0f}" if r.match_score else "—",
                "Variants Tried": tried,
                "Error": r.error or "—",
            }
        )
    df = pd.DataFrame(rows)
    results_placeholder.subheader("📊 Results Summary")
    results_placeholder.dataframe(df, use_container_width=True)

    successes = sum(1 for r in results if r.success)
    failures = len(results) - successes
    m1, m2, m3 = results_placeholder.columns(3)
    m1.metric("Total", len(results))
    m2.metric("✅ Succeeded", successes)
    m3.metric("❌ Failed", failures)

    # Download button for the full index
    repo = LocalRepository(LOCAL_REPO_DIR)
    csv_bytes = repo.summary_dataframe().to_csv(index=False).encode("utf-8")
    results_placeholder.download_button(
        "⬇️  Download index as CSV",
        data=csv_bytes,
        file_name="wikisource_index.csv",
        mime="text/csv",
    )


# Re-render persisted results on page reload
if st.session_state.results:
    _render_results(st.session_state.results)

# ---------------------------------------------------------------------------
# Extraction pipeline (runs when the button is clicked)
# ---------------------------------------------------------------------------
if run_button and raw_input.strip():
    queries = [q.strip() for q in raw_input.split(",") if q.strip()]
    if not queries:
        st.warning("Please enter at least one source name.")
        st.stop()

    st.session_state.running = True
    st.session_state.finished = False
    st.session_state.results = []
    st.session_state.log_lines = []

    fetcher = WikisourceFetcher(rate_limit_seconds=rate_limit, timeout=timeout)
    repo = LocalRepository(LOCAL_REPO_DIR)

    total = len(queries)
    progress_bar = progress_placeholder.progress(0, text="Starting…")
    log_lines: list[str] = []

    def _flush_logs() -> None:
        """Drain the log queue and append to log_lines."""
        while True:
            try:
                msg = _log_queue.get_nowait()
                log_lines.append(msg)
            except Empty:
                break

    for idx, query in enumerate(queries, start=1):
        progress_bar.progress(
            (idx - 1) / total,
            text=f"Processing {idx}/{total}: **{query}**",
        )

        result = fetcher.fetch_page(query)
        repo.save(result)
        st.session_state.results.append(result)

        # Drain log queue
        _flush_logs()

        # Append our own structured line
        if result.success:
            log_lines.append(
                f"✅ [{idx}/{total}] '{query}' → '{result.title}'"
                f" (method={result.match_method}, score={result.match_score:.0f})"
            )
        else:
            log_lines.append(
                f"❌ [{idx}/{total}] '{query}' FAILED — {result.error}"
            )
        if result.tried_variants and len(result.tried_variants) > 1:
            log_lines.append(
                "   Variants tried: " + " → ".join(result.tried_variants)
            )

        st.session_state.log_lines = log_lines.copy()

        # Update live log panel
        log_placeholder.markdown(
            "<div class='log-box'>" + "<br>".join(log_lines[-40:]) + "</div>",
            unsafe_allow_html=True,
        )

    # Final progress
    _flush_logs()
    progress_bar.progress(1.0, text=f"Done — {total} source(s) processed.")
    st.session_state.running = False
    st.session_state.finished = True

    _render_results(st.session_state.results)

elif run_button:
    st.warning("Please enter at least one source name before clicking Extract.")

# Render persisted log on page reload
if st.session_state.log_lines and not st.session_state.running:
    log_placeholder.markdown(
        "<div class='log-box'>" + "<br>".join(st.session_state.log_lines[-40:]) + "</div>",
        unsafe_allow_html=True,
    )
