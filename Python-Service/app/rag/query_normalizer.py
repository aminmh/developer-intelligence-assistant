"""Query normalization for routing.

Applied identically to route-example utterances (at RouteLayer indexing time) and
incoming user queries (at routing time) so surface-level text variation and domain
jargon don't cause spurious mismatches. See prompt_normalization.MD.

Typo correction is domain-constrained (SymSpell over a domain-only dictionary),
feature-flagged via ENABLE_TYPO_CORRECTION, and shipped disabled. See
misspelling_prompt.MD.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from infra.settings import settings
from rag.domain_dictionary import DICTIONARY_PATH, generate_domain_dictionary
import urllib.request

# Path to the domain jargon dict (relative to this module's directory).
_DOMAIN_TERMS_PATH = Path(__file__).parent.parent / "config" / "domain_terms.json"

# Characters stripped from the leading/trailing edges of the query. '?' and '!'
# are deliberately preserved (sentence-transformer embeddings tolerate them).
_EDGE_PUNCTUATION = set('.,;:"\'\t\n()[]{}<>-—_/\\')

# Max edit distance for SymSpell lookup.
_MAX_EDIT_DISTANCE = 2

# Only accept a correction whose dictionary count is at least this (avoids
# "correcting" to rare/accidental terms).
_MIN_FREQUENCY_THRESHOLD = 2


def _load_domain_terms() -> Dict[str, str]:
    try:
        with open(_DOMAIN_TERMS_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return {
            str(key).lower(): str(value)
            for key, value in data.items()
            if not str(key).startswith("_")
        }
    except Exception:
        return {}


_DOMAIN_TERMS = _load_domain_terms()

# Lazily-initialised SymSpell corrector: (sym_spell, known_terms) or None.
_corrector: Optional[Tuple[object, Set[str]]] = None


def _get_corrector() -> Optional[Tuple[object, Set[str]]]:
    """Load the domain dictionary into SymSpell (lazy, once)."""
    global _corrector
    if _corrector is None:
        try:
            from symspellpy import SymSpell  # lazy import; symspellpy is optional

            sym = SymSpell(max_dictionary_edit_distance=_MAX_EDIT_DISTANCE, prefix_length=7)
            known: Set[str] = set()
            if DICTIONARY_PATH.is_file():
                with open(DICTIONARY_PATH, "r", encoding="utf-8") as handle:
                    for line in handle:
                        parts = line.rstrip("\n").split("\t")
                        if parts and parts[0]:
                            known.add(parts[0])
                sym.load_dictionary(
                    str(DICTIONARY_PATH),
                    term_index=0,
                    count_index=1,
                    separator="\t",
                )
            _corrector = (sym, known)
        except Exception:
            _corrector = None
    return _corrector


def regenerate_domain_dictionary(qdrant_url: Optional[str] = None) -> int:
    """Rebuild the domain dictionary and drop the cached corrector.

    Called after indexing so newly added class/method names become correctable.
    Returns the number of terms written.
    """
    global _corrector
    url = qdrant_url or settings.qdrant_url
    count = generate_domain_dictionary(url)
    _corrector = None
    return count


def _correct_typos(tokens: List[str]) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Correct single-token typos against the domain dictionary.

    Known-good tokens pass through untouched. Corrections are only applied when
    there is exactly one candidate at or above the frequency threshold.
    """
    corrector = _get_corrector()
    if corrector is None:
        return tokens, []

    from symspellpy import Verbosity  # lazy import

    sym, known = corrector
    corrected: List[str] = []
    corrections: List[Tuple[str, str]] = []

    for token in tokens:
        if token in known:
            corrected.append(token)
            continue

        suggestions = sym.lookup(
            token,
            Verbosity.CLOSEST,
            max_edit_distance=_MAX_EDIT_DISTANCE,
            include_unknown=False,
        )
        if len(suggestions) == 1 and suggestions[0].count >= _MIN_FREQUENCY_THRESHOLD:
            best = suggestions[0].term
            corrected.append(best)
            corrections.append((token, best))
        else:
            corrected.append(token)

    return corrected, corrections


def _get_typo_logger() -> logging.Logger:
    logger = logging.getLogger("typo_correction")
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        handler = logging.FileHandler(Path(settings.log_dir) / "typo-corrections.log")
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s"))
        logger.addHandler(handler)
    return logger


def _log_corrections(query_id: str, corrections: List[Tuple[str, str]]) -> None:
    try:
        logger = _get_typo_logger()
        for original, corrected in corrections:
            logger.debug("query=%s: %s -> %s", query_id, original, corrected)
    except Exception:
        pass


def normalize_query(text: str) -> str:
    """Normalize a query for embedding-based routing.

    Order: NFKC unicode normalization -> lowercase -> whitespace/punctuation
    cleanup -> domain jargon expansion -> (optional) typo correction. No
    stemming/lemmatization/stopwords (they degrade sentence-transformer
    embeddings).
    """
    if not text or not isinstance(text, str):
        return ""

    query_id = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]

    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.casefold()

    # Collapse internal whitespace runs, then strip leading/trailing edge
    # punctuation while keeping ?/!.
    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = normalized.strip("".join(_EDGE_PUNCTUATION))

    if _DOMAIN_TERMS:
        # Word-boundary replacement so abbreviations don't match inside words.
        pattern = re.compile(
            r"\b(" + "|".join(re.escape(k) for k in _DOMAIN_TERMS) + r")\b"
        )
        normalized = pattern.sub(
            lambda m: _DOMAIN_TERMS[m.group(1)], normalized
        )

    if settings.enable_typo_correction:
        tokens = normalized.split()
        corrected, corrections = _correct_typos(tokens)
        if corrections:
            _log_corrections(query_id, corrections)
        normalized = " ".join(corrected)

    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized
