"""Fuzzy matching utilities for ChronoDownloader.

Provides text normalization, similarity scoring, and combined matching
logic for selecting the best provider candidates based on title and creator.
"""
from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Iterable

def strip_accents(text: str) -> str:
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

def normalize_text(text: str) -> str:
    """Normalize text for robust fuzzy matching.

    Performs lowercase conversion, accent removal, whitespace collapse,
    and punctuation stripping.

    Args:
        text: Input text to normalize

    Returns:
        Normalized text suitable for matching
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
    
    return int(round(difflib.SequenceMatcher(None, a_norm, b_norm).ratio() * 100))

def token_set_ratio(a: str, b: str) -> int:
    """Approximate token set ratio using stdlib.

    This is a simplified version of fuzzywuzzy/rapidfuzz token_set_ratio.
    Compares sorted unique tokens from both strings.

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
    
    sa = " ".join(sorted(a_tokens))
    sb = " ".join(sorted(b_tokens))
    
    return simple_ratio(sa, sb)

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

def parse_year(text: str | None) -> int | None:
    """Extract a 4-digit year from text.

    Args:
        text: Text potentially containing a year

    Returns:
        Extracted year as integer, or None if not found
    """
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
    query_creator: str | None = None,
    creators: Iterable[str] | None = None,
    creator_weight: float = 0.2,
    method: str = "token_set",
) -> float:
    """Compute a combined score 0..100 for (title, creator) matching.

    Args:
        query_title: Title from user query
        item_title: Title from provider result
        query_creator: Creator from user query (optional)
        creators: List of creators from provider result (optional)
        creator_weight: Weight for creator score (0.0 to 1.0, default 0.2)
        method: Matching method - "simple" or "token_set"

    Returns:
        Combined weighted score from 0 to 100
    """
    ts = float(title_score(query_title, item_title, method=method))
    cs = float(creator_score(query_creator, creators)) if query_creator else 0.0
    
    creator_weight = max(0.0, min(1.0, float(creator_weight or 0.0)))
    
    return ts * (1.0 - creator_weight) + cs * creator_weight
