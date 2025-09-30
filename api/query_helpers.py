"""Query string escaping utilities for ChronoDownloader.

Provides safe escaping functions for query strings used in SRU/CQL and SPARQL queries.
"""
from __future__ import annotations

import re


def escape_sru_literal(value: str | None) -> str:
    """Escape a literal for inclusion in SRU/CQL quoted phrases.

    - Escapes backslashes and double quotes.
    - Collapses newlines and tabs into spaces.

    Args:
        value: Input string to escape

    Returns:
        Escaped string safe for SRU/CQL queries
    """
    if value is None:
        return ""
    
    s = str(value)
    s = s.replace("\\", r"\\")
    s = s.replace('"', r'\"')
    s = re.sub(r"[\r\n\t]+", " ", s)
    
    return s


def escape_sparql_string(value: str | None) -> str:
    """Escape a string for safe inclusion in SPARQL single-quoted literals.

    - Escapes backslashes and single quotes.
    - Replaces newlines, carriage returns, and tabs with spaces.

    Args:
        value: Input string to escape

    Returns:
        Escaped string safe for SPARQL queries
    """
    if value is None:
        return ""
    
    s = str(value)
    s = s.replace("\\", r"\\")
    s = s.replace("'", r"\'")
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    
    return s
