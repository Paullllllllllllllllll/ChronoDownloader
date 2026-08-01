"""Shared query and record helpers for ChronoDownloader connectors.

Escaping for SRU/CQL and SPARQL query strings, plus small readers for the
MODS records the SRU endpoints return.
"""

from __future__ import annotations

import re
from typing import Any


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
    s = s.replace('"', r"\"")
    s = re.sub(r"[\r\n\t]+", " ", s)

    return s


def escape_sparql_string(value: str | None) -> str:
    """Escape a string for safe inclusion in SPARQL quoted literals.

    - Escapes backslashes, single quotes, and double quotes (``\\'`` and
      ``\\"`` are valid ECHAR escapes in both quoting styles, so the result
      is safe inside single- and double-quoted literals alike).
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
    s = s.replace('"', r"\"")
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")

    return s


def mods_creator(mods: Any, ns: dict[str, str]) -> str | None:
    """Read the creator from a MODS record's own name element.

    ``.//mods:name`` matches at any depth, so a ``<subject><name>`` heading
    was read as readily as the record's own: an anonymous Swabian cookbook
    came back credited to the cookery writer it is *about*. Only a top-level
    ``<name>`` describes the work.

    Args:
        mods: MODS element for one record
        ns: XML namespace map

    Returns:
        Creator string, or None when the record names nobody
    """
    for name_el in mods.findall("mods:name", ns):
        for path in ("mods:displayForm", "mods:namePart"):
            el = name_el.find(path, ns)
            if el is not None and el.text and el.text.strip():
                return str(el.text.strip())
    return None


def mods_date(mods: Any, ns: dict[str, str]) -> str | None:
    """Read the publication date from a MODS record.

    Args:
        mods: MODS element for one record
        ns: XML namespace map

    Returns:
        Date string as printed in the record, or None
    """
    el = mods.find("mods:originInfo/mods:dateIssued", ns)
    if el is not None and el.text and el.text.strip():
        return str(el.text.strip())
    return None
