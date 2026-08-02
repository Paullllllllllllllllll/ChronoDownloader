"""Fuzzy matching utilities for ChronoDownloader.

Provides text normalization, similarity scoring for titles and creators, and
year extraction from free-form imprint dates. Candidate ranking itself lives
in :mod:`main.orchestration.selection`, which combines these primitives with
provider- and metadata-specific boosts.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from collections.abc import Iterable


def strip_accents(text: str | None) -> str:
    """Remove accent marks from text while preserving base characters.

    Args:
        text: Input text with potential accents

    Returns:
        Text with accents removed
    """
    if text is None:
        return ""

    # Normalize and remove combining characters
    nfkd = unicodedata.normalize("NFKD", str(text))
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


# Latin letters that NFKD does not decompose (unlike e.g. e-acute) and that
# the ASCII-only filter in normalize_text would otherwise replace with a
# space, splitting a word in two ("Bønder" -> "b nder") and sinking match
# scores below the selection gate for Danish/Norwegian/German/Polish titles.
# Public because api.core.naming reuses it for directory slugs: a title must
# fold to the same slug there as it does to the same token here.
NON_DECOMPOSABLE_TRANSLIT = str.maketrans(
    {
        "ß": "ss",
        "ø": "o",
        "æ": "ae",
        "œ": "oe",
        "ł": "l",
        "đ": "d",
        "ð": "d",
        "þ": "th",
    }
)


def normalize_text(text: str | None) -> str:
    """Normalize text for robust fuzzy matching.

    Performs lowercase conversion, accent removal, transliteration of
    non-decomposable Latin letters (ß, ø, æ, œ, ł, đ, ð, þ), whitespace
    collapse, and punctuation stripping.

    Args:
        text: Input text to normalize

    Returns:
        Normalized text suitable for matching
    """
    if text is None:
        return ""

    s = strip_accents(str(text)).lower().translate(NON_DECOMPOSABLE_TRANSLIT)

    # Replace punctuation and separators with spaces
    s = re.sub(r"[\t\r\n]+", " ", s)
    ascii_only = re.sub(r"[^0-9a-z]+", " ", s)

    # Collapse multiple spaces
    normalized = re.sub(r"\s+", " ", ascii_only).strip()

    # A title written in a non-Latin script (Cyrillic, Greek, ...) is gutted by
    # the ASCII filter, so both scorers short-circuit to 0 and no such record
    # can ever clear the selection gate -- even against a byte-identical query.
    # Testing the ASCII pass for emptiness was not enough: a single surviving
    # token, typically a year or a volume number, kept the mutilated form
    # ("Podarok ... 1861" -> "1861"). Fall back whenever the source carries a
    # letter the ASCII filter cannot represent, using a Unicode-aware pass that
    # strips punctuation but keeps the script's own letters and digits.
    if normalized and not any(ch.isalpha() and not ch.isascii() for ch in s):
        return normalized

    fallback = re.sub(r"[^\w\s]|_", " ", s)
    return re.sub(r"\s+", " ", fallback).strip()


def _ratio_pct(a: str, b: str) -> int:
    """Return ``difflib``'s ratio for two already-normalized strings, in 0..100."""
    return int(round(difflib.SequenceMatcher(None, a, b).ratio() * 100))


def simple_ratio(a: str, b: str) -> int:
    """Return a similarity score in 0..100 using difflib ratio.

    Args:
        a: First string to compare
        b: Second string to compare

    Returns:
        Similarity score from 0 (no match) to 100 (perfect match)
    """
    a_norm = normalize_text(a)
    b_norm = normalize_text(b)

    if not a_norm or not b_norm:
        return 0

    return _ratio_pct(a_norm, b_norm)


def token_set_ratio(a: str, b: str) -> int:
    """Return rapidfuzz-style token SET ratio in 0..100, built on stdlib difflib.

    Follows rapidfuzz's set semantics: the shared tokens (``t0``) are compared
    against each side's full sorted token string (``t1``, ``t2``), and the best
    of the three pairings wins. The consequence that matters here is that a
    query which is a pure token subset of the candidate scores 100 -- the
    normal case for this tool, whose CSV query column is ``short_title`` while
    catalog records carry the full imprint title.

    With no shared tokens at all, ``t0`` is empty and the comparison degrades
    to the plain token-SORT ratio of the two sides.

    Args:
        a: First string to compare
        b: Second string to compare

    Returns:
        Similarity score from 0 (no match) to 100 (perfect match)
    """
    a_tokens = set(normalize_text(a).split())
    b_tokens = set(normalize_text(b).split())

    if not a_tokens or not b_tokens:
        return 0

    intersection = sorted(a_tokens & b_tokens)
    # t1/t2 are the fully sorted token strings of each side, with the shared
    # tokens hoisted to the front; both are non-empty because their side is.
    t0 = " ".join(intersection)
    t1 = " ".join(intersection + sorted(a_tokens - b_tokens))
    t2 = " ".join(intersection + sorted(b_tokens - a_tokens))

    # The tokens are already fully normalized (normalize_text is idempotent on
    # its own output), so compute the difflib ratios directly. An empty t0
    # carries no information and is skipped rather than scored as a mismatch.
    if not t0:
        return _ratio_pct(t1, t2)

    return max(_ratio_pct(t0, t1), _ratio_pct(t0, t2), _ratio_pct(t1, t2))


def title_score(query_title: str, item_title: str, method: str = "token_set") -> int:
    """Compute similarity score between query and item titles.

    Args:
        query_title: Title from user query
        item_title: Title from provider result
        method: Matching method - "simple" or "token_set" (default)

    Returns:
        Similarity score from 0 to 100
    """
    if method == "simple":
        return simple_ratio(query_title, item_title)

    # Default to token_set
    return token_set_ratio(query_title, item_title)


def creator_score(query_creator: str | None, creators: Iterable[str] | None) -> int:
    """Compute best similarity score between query creator and item creators.

    Args:
        query_creator: Creator from user query
        creators: List of creators from provider result

    Returns:
        Best similarity score from 0 to 100 (0 if either input is empty)
    """
    if not query_creator:
        return 0

    if not creators:
        return 0

    best = 0
    for c in creators:
        best = max(best, token_set_ratio(query_creator, c))

    return best


# Bounds of a plausible imprint year. The lower bound keeps four-digit shelf
# marks, page counts, and volume numbers out; the upper bound leaves room for
# a catalogue record dated slightly in the future without admitting a
# five-digit run (the \b anchors already exclude those).
MIN_PLAUSIBLE_YEAR = 1000
MAX_PLAUSIBLE_YEAR = 2100

_YEAR_RE = re.compile(r"\b(\d{4})\b")


def extract_year(value: object) -> int | None:
    """Extract a plausible four-digit year from a free-form value.

    Returns the FIRST four-digit run in ``[MIN_PLAUSIBLE_YEAR,
    MAX_PLAUSIBLE_YEAR]``, so a range ("1651-1660", "1651/52") yields its
    opening year and an ISO date ("2019-03-01") yields its year. Values are
    stringified first, which covers integer and float cells read from a CSV
    ("1651", "1651.0") as well as ``NaN``.

    Deliberately fails open: anything without such a run -- ``None``, an empty
    string, a Roman-numeral century ("S. XVIII"), a verbal date ("18. Jh."),
    or plain garbage -- returns ``None``. Callers treat ``None`` as "no year
    information", never as a mismatch, so early modern imprints with messy or
    absent dates are not disadvantaged.

    Args:
        value: Any value that may carry a year (str, int, float, None).

    Returns:
        The extracted year, or None when no plausible year is present.
    """
    if value is None:
        return None

    for match in _YEAR_RE.finditer(str(value)):
        year = int(match.group(1))
        if MIN_PLAUSIBLE_YEAR <= year <= MAX_PLAUSIBLE_YEAR:
            return year

    return None
