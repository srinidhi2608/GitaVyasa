# GitaVyasa

## Project Overview
GitaVyasa is a computational tool with two complementary capabilities:

1. **Wikisource Text Extractor** — automates the bulk downloading of Sanskrit / Indic texts from [English Wikisource](https://en.wikisource.org) into a structured local repository.  It uses smart phonetic and fuzzy-matching to handle the many valid English transliterations of Indian names (e.g. *Shiva* vs. *Siva*, *Advaita* vs. *Adwaita*).

2. **Commentary Analyser** — explores and compares commentaries on the Bhagavad Gita by Shankara, Ramanuja, and Madhva, providing insights and visualisations.

---

## Installation

Requires **Python 3.11+**.

```bash
git clone https://github.com/srinidhi2608/GitaVyasa.git
cd GitaVyasa
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### Optional: Tesseract OCR (for PDF extraction)
| Platform | Command |
|----------|---------|
| Ubuntu   | `sudo apt install tesseract-ocr` |
| macOS    | `brew install tesseract` |
| Windows  | Download from [UB Mannheim](https://github.com/UB-Mannheim/tesseract/wiki) |

---

## Running the Application

```bash
streamlit run main_app.py
```

Open the URL shown in the terminal (usually `http://localhost:8501`).

---

## Using the Wikisource Extractor

1. Enter comma-separated source names in the text area, e.g.:
   ```
   Bhagavad Gita, Mahabharata, Shiva Purana, Ramayana, Upanishads
   ```
2. Adjust the sidebar settings (rate-limit, timeout, fuzzy-match threshold).
3. Click **▶ Extract**.
4. Watch the real-time progress bar and log panel.
   - The log shows exactly which alternate spellings were tried when an exact match failed.
5. Review the success/failure summary table.
6. Download the full index as CSV with the **⬇️ Download** button.

Downloaded texts are saved to `data/wikisource/<title>/`:
```
data/wikisource/
    Bhagavad_Gita/
        content.txt       ← raw wikitext
        metadata.json     ← title, URL, match method, score, variants tried
    index.db              ← SQLite index for quick look-ups
```

---

## Project Structure

```
GitaVyasa/
├── main_app.py           # Streamlit UI (Wikisource extractor + commentary analyser)
├── wikisource_fetcher.py # Wikisource / Wikimedia API client with rate-limiting
├── smart_search.py       # Phonetic (Soundex/Metaphone) + fuzzy (RapidFuzz) matching
├── storage.py            # Local repository manager (files + SQLite)
├── data_processor.py     # PDF ingestion and verse-level structuring
├── nlp_analyzer.py       # Sanskrit NLP analysis helpers
├── config.py             # Project-wide configuration
├── requirements.txt      # Python dependencies
├── utils/
│   ├── __init__.py
│   └── sanskrit_processor.py
└── data/
    ├── wikisource/       # Created automatically at runtime
    ├── pdfs/             # Place commentary PDFs here
    ├── processed/        # Structured CSV output from data_processor
    └── verse_map.csv
```

---

## Key Dependencies

| Package | Purpose |
|---------|---------|
| `streamlit` | Web UI |
| `requests` | Wikimedia REST API calls |
| `rapidfuzz` | Fuzzy string matching for transliteration variants |
| `jellyfish` | Soundex / Metaphone phonetic matching |
| `pandas` | Data manipulation and summary tables |
| `PyPDF2` / `pytesseract` | PDF text extraction (commentary pipeline) |

---

## Known Limitations
- Sanskrit NLP (sandhi-splitting, lemmatisation) uses placeholder implementations; production-grade processing would require a dedicated Sanskrit NLP library.
- Wikisource coverage of Sanskrit texts is incomplete; some queries will return no results.

## Contact
For questions or contributions, please open an issue or pull request in this repository.
