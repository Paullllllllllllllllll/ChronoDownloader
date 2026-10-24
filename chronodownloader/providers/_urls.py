"""URL reading for the adapters' ``item_id_from_url`` hooks.

Each hook first checks that the URL lies on one of its library's hosts
(:func:`on_hosts`), then matches the library's item, viewer or manifest
shape against the path (:func:`path_match`) or a query parameter
(:func:`query_value`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import parse_qs, unquote, urlsplit

__all__ = ["on_hosts", "path_match", "query_value"]


def on_hosts(url: str, hosts: Iterable[str]) -> bool:
    """Return True when *url* is http(s) on one of *hosts* or a subdomain."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https"):
        return False
    host = (parts.hostname or "").rstrip(".").lower()
    return any(host == known or host.endswith(f".{known}") for known in hosts)


def path_match(url: str, pattern: re.Pattern[str]) -> str | None:
    """Return the first group of *pattern* searched in *url*'s decoded path."""
    try:
        path = unquote(urlsplit(url.strip()).path)
    except ValueError:
        return None
    match = pattern.search(path)
    return match.group(1) if match else None


def query_value(url: str, name: str) -> str | None:
    """Return the first value of query parameter *name* (case-insensitive)."""
    try:
        query = parse_qs(urlsplit(url.strip()).query)
    except ValueError:
        return None
    for key, values in query.items():
        if key.lower() == name.lower() and values and values[0].strip():
            return values[0].strip()
    return None
