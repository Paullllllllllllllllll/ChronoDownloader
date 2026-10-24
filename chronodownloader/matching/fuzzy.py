"""The text score of a candidate against the sought work.

Ported from v1.30.3: the scorers from ``api/matching.py``, the combination
and the year penalty from ``score_candidate`` and ``year_penalty`` in
``main/orchestration/selection.py``. For the same inputs every component
has the value v1.30.3 computed; ``tests/matching/test_fuzzy.py`` recomputes
the stored scores of all jev_eval fixture pairs.

The score of a record against a query (:func:`score`):

- ``title``: the token-set ratio (0 to 100) of the query title against the
  record title, so a short title that is a token subset of the full imprint
  title scores 100;
- ``creator_bonus``: :data:`CREATOR_WEIGHT` times the best token-set ratio
  of the query creator against the record's creators (0 without a query
  creator), at most 20;
- ``boost``: :data:`IIIF_BOOST` for a IIIF manifest plus
  :data:`ITEM_URL_BOOST` for an item URL, at most 3.5;
- ``year_penalty``: :data:`YEAR_PENALTY_PER_YEAR` per year by which the
  candidate year lies outside :data:`YEAR_TOLERANCE` years around the query
  year, capped at :data:`MAX_YEAR_PENALTY`; 0 when either year is unknown;
- ``total = title + creator_bonus + boost - year_penalty``, from -25 to
  123.5. The matching thresholds (``judge.thresholds.text_*``) apply to it.

Two differences from v1.30.3 lie outside the score values. The candidate
year is the record's ``year``, else :func:`~chronodownloader.providers.
_query.parse_year` of its ``date`` (years 1400 to 2100, four digits not
adjacent to other digits), where v1.30.3 read years 1000 to 2100 between
word boundaries; the two agree on every fixture date. ``candidate_year`` is
reported whether or not the query has a year.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from chronodownloader.output.paths import NON_DECOMPOSABLE_TRANSLIT, strip_accents
from chronodownloader.providers._query import parse_year
from chronodownloader.providers.base import SearchQuery, SearchRecord

__all__ = [
    "CREATOR_WEIGHT",
    "IIIF_BOOST",
    "ITEM_URL_BOOST",
    "MAX_YEAR_PENALTY",
    "YEAR_PENALTY_PER_YEAR",
    "YEAR_TOLERANCE",
    "TextScore",
    "candidate_year",
    "creator_score",
    "normalize_text",
    "score",
    "title_score",
    "token_set_ratio",
    "year_penalty",
]

CREATOR_WEIGHT: Final = 0.2
"""Weight of the creator ratio in the total (v1.30.3 ``creator_weight``)."""

YEAR_TOLERANCE: Final = 15
"""Half-width in years of the window without year penalty."""

YEAR_PENALTY_PER_YEAR: Final = 0.5
MAX_YEAR_PENALTY: Final = 25.0
IIIF_BOOST: Final = 3.0
ITEM_URL_BOOST: Final = 0.5

_CONTROL_WHITESPACE = re.compile(r"[\t\r\n]+")
_NON_ASCII_ALNUM = re.compile(r"[^0-9a-z]+")
_NON_WORD = re.compile(r"[^\w\s]|_")
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True)
class TextScore:
    """The components of a candidate's text score and their total."""

    title: float
    creator: float
    creator_bonus: float
    boost: float
    year_penalty: float
    candidate_year: int | None
    total: float


def normalize_text(text: str | None) -> str:
    """Return *text* folded for matching.

    Lowercase, accents stripped, non-decomposable Latin letters
    transliterated (``ß``, ``ø``, ``æ``, ``œ``, ``ł``, ``đ``, ``ð``, ``þ``),
    punctuation replaced by spaces, whitespace collapsed. A text holding a
    letter outside ASCII after that folding (Cyrillic, Greek) keeps its own
    letters and digits instead of losing them to the ASCII filter.
    """
    if text is None:
        return ""
    s = strip_accents(str(text)).lower().translate(NON_DECOMPOSABLE_TRANSLIT)
    s = _CONTROL_WHITESPACE.sub(" ", s)
    normalized = _SPACES.sub(" ", _NON_ASCII_ALNUM.sub(" ", s)).strip()
    if normalized and not any(ch.isalpha() and not ch.isascii() for ch in s):
        return normalized
    return _SPACES.sub(" ", _NON_WORD.sub(" ", s)).strip()


def _ratio_pct(a: str, b: str) -> int:
    return int(round(difflib.SequenceMatcher(None, a, b).ratio() * 100))


def token_set_ratio(a: str | None, b: str | None) -> int:
    """Return the token-set ratio of *a* and *b*, from 0 to 100.

    The shared tokens are compared with each side's sorted tokens (shared
    ones first) and the best of the three pairings counts, so a pure token
    subset scores 100. Without shared tokens it is the ratio of the two
    sorted token strings; an empty side scores 0.
    """
    a_tokens = set(normalize_text(a).split())
    b_tokens = set(normalize_text(b).split())
    if not a_tokens or not b_tokens:
        return 0
    shared = sorted(a_tokens & b_tokens)
    t0 = " ".join(shared)
    t1 = " ".join(shared + sorted(a_tokens - b_tokens))
    t2 = " ".join(shared + sorted(b_tokens - a_tokens))
    if not t0:
        return _ratio_pct(t1, t2)
    return max(_ratio_pct(t0, t1), _ratio_pct(t0, t2), _ratio_pct(t1, t2))


def title_score(query_title: str | None, item_title: str | None) -> int:
    """Return the token-set ratio of the two titles."""
    return token_set_ratio(query_title, item_title)


def creator_score(query_creator: str | None, creators: Iterable[str] | None) -> int:
    """Return the best token-set ratio of *query_creator* against *creators*.

    0 when either side is empty.
    """
    if not query_creator or not creators:
        return 0
    return max((token_set_ratio(query_creator, c) for c in creators), default=0)


def year_penalty(
    query_year: int | None,
    candidate_year: int | None,
    tolerance: int = YEAR_TOLERANCE,
) -> float:
    """Return the ranking penalty of a candidate dated outside the window.

    No penalty within *tolerance* years of *query_year* (bounds included);
    beyond, :data:`YEAR_PENALTY_PER_YEAR` per year of excess, capped at
    :data:`MAX_YEAR_PENALTY`. A negative tolerance counts as 0; an unknown
    year on either side gives 0, so an undated candidate is never ranked
    above a dated one that matches.
    """
    if query_year is None or candidate_year is None:
        return 0.0
    excess = abs(candidate_year - query_year) - max(0, tolerance)
    if excess <= 0:
        return 0.0
    return min(MAX_YEAR_PENALTY, YEAR_PENALTY_PER_YEAR * excess)


def candidate_year(record: SearchRecord) -> int | None:
    """Return the record's year, else the year parsed from its date."""
    if record.year is not None:
        return record.year
    return parse_year(record.date)


def score(query: SearchQuery, record: SearchRecord) -> TextScore:
    """Return the text score of *record* against *query*."""
    title = float(title_score(query.title, record.title))
    creator = (
        float(creator_score(query.creator, record.creators)) if query.creator else 0.0
    )
    creator_bonus = CREATOR_WEIGHT * creator
    boost = 0.0
    if record.iiif_manifest:
        boost += IIIF_BOOST
    if record.item_url:
        boost += ITEM_URL_BOOST
    year = candidate_year(record)
    penalty = year_penalty(query.year, year)
    return TextScore(
        title=title,
        creator=creator,
        creator_bonus=creator_bonus,
        boost=boost,
        year_penalty=penalty,
        candidate_year=year,
        total=title + creator_bonus + boost - penalty,
    )
