"""
smart_search.py — Indic-Name Smart Search

Provides phonetic (Soundex / Double-Metaphone via jellyfish) and fuzzy
(RapidFuzz) fallback matching so that transliteration variants of Sanskrit /
Indic names (e.g. "Shiva" vs. "Siva", "Advaita" vs. "Adwaita") are handled
gracefully when querying Wikisource.

Public API
----------
SmartSearchEngine(candidate_titles: list[str])
    .find_best_match(query: str) -> MatchResult | None
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

try:
    import jellyfish  # Soundex + Double Metaphone
    _JELLYFISH_AVAILABLE = True
except ImportError:  # pragma: no cover
    _JELLYFISH_AVAILABLE = False
    logging.warning("jellyfish not installed – phonetic matching disabled")

try:
    from rapidfuzz import fuzz, process as rf_process  # type: ignore
    _RAPIDFUZZ_AVAILABLE = True
except ImportError:  # pragma: no cover
    _RAPIDFUZZ_AVAILABLE = False
    logging.warning("rapidfuzz not installed – fuzzy matching disabled")


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Indic transliteration normalisation map
# Maps common variant spellings to a canonical ASCII form so that phonetic
# algorithms (designed for English) work better on transliterated text.
# ---------------------------------------------------------------------------
_INDIC_CHAR_MAP: dict[str, str] = {
    # Common spelling swaps (apply first so later rules see the canonical form)
    "w": "v",       # Adwaita -> Advaita
    # Sibilants
    "shh": "s",
    "sh": "s",      # Shiva -> Siva
    # Aspirated consonants -> base consonants
    "bh": "b", "gh": "g", "kh": "k", "ph": "p",
    # Retroflex / dental equivalence
    "tt": "t", "dd": "d", "nn": "n",
    # Long vowel variants only (safe; do NOT collapse diphthongs like ai/au)
    "aa": "a", "ii": "i", "uu": "u",
}

# Known synonym / alias groups for well-known Indic entities
_ALIAS_GROUPS: list[list[str]] = [
    ["Shiva", "Siva", "Shiv", "Siv"],
    ["Vishnu", "Visnu", "Vaishnava"],
    ["Advaita", "Adwaita", "Adwait", "Advait"],
    ["Dvaita", "Dwaita", "Dwait"],
    ["Vishishta", "Vishishtha", "Visishtadvaita", "Vishishtadvaita"],
    ["Krishna", "Krsna", "Krushna", "Krishn"],
    ["Ramayana", "Ramayan", "Ramayanam"],
    ["Mahabharata", "Mahabharat", "Mahabharatam"],
    ["Bhagavad Gita", "Bhagavadgita", "Bhagvad Gita", "Bhagwad Gita", "Gitopanishad"],
    ["Shankaracharya", "Shankara", "Sankara", "Adi Shankara", "Adi Sankara"],
    ["Ramanuja", "Ramanujacharya"],
    ["Madhvacharya", "Madhva", "Madhwa"],
    ["Brahma", "Brahman", "Brahmin"],
    ["Upanishad", "Upanishads", "Upanisad"],
    ["Rig Veda", "Rigveda", "Rig-veda"],
    ["Sama Veda", "Samaveda", "Sama-veda"],
    ["Yajur Veda", "Yajurveda", "Yajur-veda"],
    ["Atharva Veda", "Atharvaveda", "Atharva-veda"],
]


def _normalise(text: str) -> str:
    """Lower-case and apply the Indic character map to a string."""
    t = text.lower().strip()
    for src, tgt in _INDIC_CHAR_MAP.items():
        t = t.replace(src, tgt)
    return t


def _soundex(text: str) -> str:
    if _JELLYFISH_AVAILABLE:
        return jellyfish.soundex(text)
    return text


def _metaphone(text: str) -> str:
    if _JELLYFISH_AVAILABLE:
        return jellyfish.metaphone(text)
    return text


def _phonetic_codes(text: str) -> tuple[str, str]:
    """Return two phonetic codes for *text* (Soundex + NYSIIS)."""
    if _JELLYFISH_AVAILABLE:
        return (_soundex(text), jellyfish.nysiis(text))
    return (text, text)


def generate_query_variants(query: str) -> list[str]:
    """
    Return all spelling variants of *query* that SmartSearchEngine would try
    (aliases, normalised spellings).

    Module-level convenience function — does not require a SmartSearchEngine
    instance or a list of candidates.
    """
    variants: list[str] = [query]
    # Alias lookup using the shared alias groups
    ql = query.lower()
    for group in _ALIAS_GROUPS:
        for name in group:
            if name.lower() == ql:
                variants.extend(n for n in group if n != name)
                break
    # Normalised spelling variant
    norm = _normalise(query)
    if norm != query.lower():
        variants.append(norm)
    return list(dict.fromkeys(variants))  # deduplicate, preserve order


@dataclass
class MatchResult:
    """Represents the outcome of a smart-search lookup."""
    query: str
    matched_title: str
    method: str          # "exact", "alias", "phonetic", "fuzzy", "normalised"
    score: float         # 0–100
    tried: list[str] = field(default_factory=list)  # all variants attempted


class SmartSearchEngine:
    """
    Given a list of candidate page titles (retrieved from Wikisource search),
    find the best match for an arbitrary query that may use variant spellings.

    Parameters
    ----------
    candidate_titles : list[str]
        Titles to search against (e.g. from a Wikisource prefix-search).
    score_threshold : float
        Minimum fuzzy-match score (0–100) to accept a result.
    """

    def __init__(self, candidate_titles: list[str], score_threshold: float = 65.0):
        self.candidates = candidate_titles
        self.threshold = score_threshold
        self._alias_map: dict[str, list[str]] = self._build_alias_map()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def find_best_match(self, query: str) -> Optional[MatchResult]:
        """
        Try to find the best-matching title for *query*, logging every
        alternative spelling that is tried.

        Strategy (in order):
        1. Exact match (case-insensitive)
        2. Alias / synonym group lookup
        3. Normalised-spelling exact match
        4. Phonetic match (Soundex / Metaphone)
        5. Fuzzy token-sort match (RapidFuzz)
        """
        tried: list[str] = [query]

        # 1. Exact match
        result = self._exact_match(query)
        if result:
            logger.info("[smart_search] Exact match: '%s'", result)
            return MatchResult(query, result, "exact", 100.0, tried)

        # 2. Alias lookup
        aliases = self._get_aliases(query)
        for alias in aliases:
            if alias not in tried:
                tried.append(alias)
            result = self._exact_match(alias)
            if result:
                logger.info("[smart_search] Alias match '%s' -> '%s'", alias, result)
                return MatchResult(query, result, "alias", 100.0, tried)

        # 3. Normalised spelling
        norm_query = _normalise(query)
        tried.append(f"[normalised] {norm_query}")
        norm_candidates = {c: _normalise(c) for c in self.candidates}
        for title, norm_title in norm_candidates.items():
            if norm_query == norm_title:
                logger.info("[smart_search] Normalised match: '%s'", title)
                return MatchResult(query, title, "normalised", 90.0, tried)

        # 4. Phonetic match
        if _JELLYFISH_AVAILABLE:
            soundex_query = _soundex(query)
            meta_query = _metaphone(query)
            tried.append(f"[soundex] {soundex_query}")
            tried.append(f"[metaphone] {meta_query}")
            for title in self.candidates:
                if _soundex(title) == soundex_query or _metaphone(title) == meta_query:
                    logger.info("[smart_search] Phonetic match: '%s'", title)
                    return MatchResult(query, title, "phonetic", 80.0, tried)

        # 5. Fuzzy match
        if _RAPIDFUZZ_AVAILABLE and self.candidates:
            best = rf_process.extractOne(
                query,
                self.candidates,
                scorer=fuzz.token_sort_ratio,
                score_cutoff=self.threshold,
            )
            if best:
                logger.info("[smart_search] Fuzzy match: '%s' (score %.1f)", best[0], best[1])
                tried.append(f"[fuzzy] {best[0]} ({best[1]:.0f}%)")
                return MatchResult(query, best[0], "fuzzy", best[1], tried)

        logger.warning("[smart_search] No match found for '%s'", query)
        return None

    def generate_variants(self, query: str) -> list[str]:
        """Return all spelling variants of *query* that will be tried."""
        return generate_query_variants(query)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _exact_match(self, query: str) -> Optional[str]:
        ql = query.lower()
        for title in self.candidates:
            if title.lower() == ql:
                return title
        return None

    def _get_aliases(self, query: str) -> list[str]:
        ql = query.lower()
        return self._alias_map.get(ql, [])

    def _build_alias_map(self) -> dict[str, list[str]]:
        alias_map: dict[str, list[str]] = {}
        for group in _ALIAS_GROUPS:
            for name in group:
                others = [n for n in group if n != name]
                alias_map.setdefault(name.lower(), []).extend(others)
        return alias_map
