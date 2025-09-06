import difflib
import re
import unicodedata
from typing import Iterable, List, Optional


def strip_accents(text: str) -> str:
    if text is None:
        return ""
    # Normalize and remove combining characters
    nfkd = unicodedata.normalize("NFKD", str(text))
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def normalize_text(text: str) -> str:
    """Lowercase, remove accents, collapse whitespace, and strip punctuation-like chars.

    This is intended for robust fuzzy matching across providers.
    """
    if text is None:
        return ""
    s = strip_accents(str(text)).lower()
    # Replace punctuation and separators with spaces
    s = re.sub(r"[\t\r\n]+", " ", s)
    s = re.sub(r"[^0-9a-z]+", " ", s)
    # Collapse multiple spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s


def simple_ratio(a: str, b: str) -> int:
    """Return a similarity score in 0..100 using difflib ratio."""
    a_norm = normalize_text(a)
    b_norm = normalize_text(b)
    if not a_norm or not b_norm:
        return 0
    return int(round(difflib.SequenceMatcher(None, a_norm, b_norm).ratio() * 100))


def token_set_ratio(a: str, b: str) -> int:
    """Approximate token set ratio using stdlib.

    This is a simplified version of fuzzywuzzy/rapidfuzz token_set_ratio.
    """
    a_tokens = set(normalize_text(a).split())
    b_tokens = set(normalize_text(b).split())
    if not a_tokens or not b_tokens:
        return 0
    sa = " ".join(sorted(a_tokens))
    sb = " ".join(sorted(b_tokens))
    return simple_ratio(sa, sb)


def title_score(query_title: str, item_title: str, method: str = "token_set") -> int:
    if method == "simple":
        return simple_ratio(query_title, item_title)
    # default to token_set
    return token_set_ratio(query_title, item_title)


def creator_score(query_creator: Optional[str], creators: Optional[Iterable[str]]) -> int:
    if not query_creator:
        return 0
    if not creators:
        return 0
    best = 0
    for c in creators:
        best = max(best, token_set_ratio(query_creator, c))
    return best


def parse_year(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"\b(\d{4})\b", str(text))
    if not m:
        return None
    try:
        return int(m.group(1))
    except Exception:
        return None


def combined_match_score(
    query_title: str,
    item_title: str,
    query_creator: Optional[str] = None,
    creators: Optional[Iterable[str]] = None,
    creator_weight: float = 0.2,
    method: str = "token_set",
) -> float:
    """Compute a combined score 0..100 for (title, creator).

    creator_weight controls the contribution of the creator score.
    """
    ts = float(title_score(query_title, item_title, method=method))
    cs = float(creator_score(query_creator, creators)) if query_creator else 0.0
    creator_weight = max(0.0, min(1.0, float(creator_weight or 0.0)))
    return ts * (1.0 - creator_weight) + cs * creator_weight
