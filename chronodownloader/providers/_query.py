"""Query escaping, MODS readers and year parsing shared by the adapters.

The escapes and MODS readers are ported from v1.30.3
``api/query_helpers.py``, behavior unchanged; :func:`parse_year` is new.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from xml.etree.ElementTree import Element

__all__ = [
    "YEAR_MAX",
    "YEAR_MIN",
    "escape_sparql_string",
    "escape_sru_literal",
    "mods_creator",
    "mods_date",
    "parse_year",
]

YEAR_MIN = 1400
YEAR_MAX = 2100
"""The range of a plausible publication year."""

_FOUR_DIGITS = re.compile(r"(?<![0-9])[0-9]{4}(?![0-9])")


def parse_year(text: str | None) -> int | None:
    """Return the first plausible year in a provider's date text, or None.

    A year is a run of exactly four digits (``1850s`` and ``ca.1850`` give
    1850; ``18501`` gives nothing) between :data:`YEAR_MIN` and
    :data:`YEAR_MAX`; runs outside that range are skipped.
    """
    for match in _FOUR_DIGITS.finditer(text or ""):
        year = int(match.group())
        if YEAR_MIN <= year <= YEAR_MAX:
            return year
    return None


def escape_sru_literal(value: str | None) -> str:
    """Escape *value* for a quoted SRU/CQL phrase.

    Backslashes and double quotes are escaped; runs of newlines and tabs
    become one space. None gives an empty string.
    """
    if value is None:
        return ""
    escaped = str(value).replace("\\", r"\\").replace('"', r"\"")
    return re.sub(r"[\r\n\t]+", " ", escaped)


def escape_sparql_string(value: str | None) -> str:
    r"""Escape *value* for a quoted SPARQL literal.

    Backslashes, single and double quotes are escaped (``\'`` and ``\"``
    are valid in both quoting styles); newlines, carriage returns and tabs
    become spaces. None gives an empty string.
    """
    if value is None:
        return ""
    escaped = str(value).replace("\\", r"\\").replace("'", r"\'").replace('"', r"\"")
    return escaped.replace("\r", " ").replace("\n", " ").replace("\t", " ")


def mods_creator(mods: Element, ns: Mapping[str, str]) -> str | None:
    """Return the creator from the MODS record's own top-level name element.

    Only a top-level ``<name>`` describes the work: ``.//mods:name`` would
    also match a ``<subject><name>`` heading and credit the book to the person
    it is about.
    """
    for name_el in mods.findall("mods:name", dict(ns)):
        for path in ("mods:displayForm", "mods:namePart"):
            el = name_el.find(path, dict(ns))
            if el is not None and el.text and el.text.strip():
                return el.text.strip()
    return None


def mods_date(mods: Element, ns: Mapping[str, str]) -> str | None:
    """Return the publication date as printed in the MODS record, or None."""
    el = mods.find("mods:originInfo/mods:dateIssued", dict(ns))
    if el is not None and el.text and el.text.strip():
        return el.text.strip()
    return None
