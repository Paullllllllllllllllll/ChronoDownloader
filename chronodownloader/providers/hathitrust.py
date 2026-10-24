"""HathiTrust: identifier lookups in the Bibliographic API; metadata only.

Ported from ChronoDownloader v1.30.3 ``api/providers/hathitrust.py`` on
19.10.2026. Changed: the adapter is declared metadata-only
(``Capabilities(downloads=False)``), so ``download`` and ``pages`` raise
:class:`UnsupportedCapability` instead of saving the bibliographic record and
returning False; the key ``HATHI_API_KEY`` is not read (page images need
OAuth 1.0a-signed Data API requests, which the tool does not implement); a
failed lookup raises :class:`ProviderBlocked` instead of yielding no
candidates, while a 404 for one identifier means the library does not hold
it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar

from chronodownloader.net import BreakerOpen, Cause, Network, RequestFailed
from chronodownloader.settings import Settings

from ._query import parse_year
from ._urls import on_hosts, path_match, query_value
from .base import (
    Capabilities,
    DownloadRequest,
    DownloadResult,
    InvalidResponse,
    PageRef,
    ProviderBlocked,
    SearchQuery,
    SearchRecord,
    UnsupportedCapability,
    provider_error,
)

__all__ = [
    "BIB_BASE_URL",
    "ID_ORDER",
    "KEY",
    "HathiTrust",
    "bib_url",
    "build",
    "parse_bib_response",
    "parse_identifiers",
]

logger = logging.getLogger(__name__)

KEY = "hathitrust"

BIB_BASE_URL = "https://catalog.hathitrust.org/api/volumes/brief/"

ID_ORDER = ("htid", "oclc", "isbn", "lccn", "issn")
"""The identifier hints in lookup order."""

_HINTS = (
    ("oclc", re.compile(r"oclc\s*:\s*(\d+)", re.IGNORECASE)),
    ("isbn", re.compile(r"isbn\s*:\s*([0-9Xx\-]+)", re.IGNORECASE)),
    ("lccn", re.compile(r"lccn\s*:\s*([0-9A-Za-z\-/.]+)", re.IGNORECASE)),
    ("issn", re.compile(r"issn\s*:\s*(\d{4}-?\d{3}[0-9Xx])", re.IGNORECASE)),
    ("htid", re.compile(r"htid\s*:\s*([^\s,;]+)", re.IGNORECASE)),
)
_MANIFEST_HTID = re.compile(r"/cgi/imgsrv/manifest/([^?#]+)")
_CATALOG_RECORD = re.compile(r"^/Record/(\d+)")


def bib_url(id_type: str, value: str, base_url: str = BIB_BASE_URL) -> str:
    """Return the Bibliographic API URL of one identifier."""
    return f"{base_url}{id_type.lower().strip()}/{value}.json"


def parse_identifiers(text: str | None) -> dict[str, list[str]]:
    """Return the identifier hints in *text* (``oclc:12345 isbn:978...``).

    ISBN hyphens are dropped; types without a hint are left out.
    """
    if not text:
        return {}
    hints: dict[str, list[str]] = {}
    for id_type, pattern in _HINTS:
        values = [match.group(1) for match in pattern.finditer(str(text))]
        if id_type == "isbn":
            values = [value.replace("-", "") for value in values]
        if values:
            hints[id_type] = values
    return hints


def _first_text(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value) if value else ""


def _date(record: Mapping[str, Any]) -> str | None:
    for name in ("pubDate", "publishDates", "date"):
        value = record.get(name)
        if isinstance(value, list) and value:
            return str(value[0])
        if isinstance(value, (str, int)):
            return str(value) or None
    return None


def _items_by_record(data: Mapping[str, Any]) -> dict[str, list[Mapping[str, Any]]]:
    """Group the top-level ``items`` by their ``fromRecord`` number."""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    items = data.get("items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, Mapping):
                grouped.setdefault(str(item.get("fromRecord")), []).append(item)
    return grouped


def _record(record_id: str, bib: Mapping[str, Any], items: list[Any]) -> SearchRecord:
    # Never substitute the query title for an untitled record: it would
    # match the query perfectly. An empty title is judged like any other.
    title = _first_text(bib.get("titles") or bib.get("title") or [])
    authors = bib.get("authors") or bib.get("mainAuthor") or []
    if isinstance(authors, str):
        authors = [authors]
    date = _date(bib)
    htid: str | None = None
    item_url: str | None = None
    for item in items:
        if isinstance(item, Mapping):
            htid = htid or (str(item["htid"]) if item.get("htid") else None)
            item_url = item_url or (
                str(item["itemURL"]) if item.get("itemURL") else None
            )
    raw = {"record_id": record_id, "htid": htid, "item_url": item_url, "bib": bib}
    return SearchRecord(
        provider=KEY,
        source_id=htid or record_id,
        title=title,
        creators=tuple(str(author) for author in authors),
        date=date,
        year=parse_year(date),
        item_url=item_url,
        raw=MappingProxyType(raw),
    )


def parse_bib_response(data: Mapping[str, Any], limit: int) -> list[SearchRecord]:
    """Return at most *limit* records of a Bibliographic API answer.

    The answer holds ``records`` and ``items`` as sibling top-level keys;
    each item names its record in ``fromRecord``. A record's own ``items``
    list is the fallback. Malformed records are skipped.
    """
    records = data.get("records")
    if not isinstance(records, Mapping):
        return []
    grouped = _items_by_record(data)
    out: list[SearchRecord] = []
    for record_id, bib in records.items():
        if len(out) >= limit:
            break
        if not isinstance(bib, Mapping):
            logger.warning("%s: skipping malformed record %s", KEY, record_id)
            continue
        own_items = bib.get("items")
        items = grouped.get(str(record_id)) or (
            own_items if isinstance(own_items, list) else []
        )
        out.append(_record(str(record_id), bib, items))
    return out


class HathiTrust:
    """The HathiTrust adapter: metadata only.

    Search runs only when the title or creator carries an identifier hint
    (``htid:``, ``oclc:``, ``isbn:``, ``lccn:``, ``issn:``); HathiTrust has
    no public keyword API. An ``htid:`` hint becomes a record without a
    request, titled with the query title. A record carries two identifiers;
    ``source_id`` is the volume's htid, else the catalog record number, and
    both stay in ``raw`` with the bibliographic record (``bib``).
    """

    hosts: ClassVar[frozenset[str]] = frozenset({"hathitrust.org"})

    def __init__(self, bib_base_url: str = BIB_BASE_URL) -> None:
        self._bib_base_url = bib_base_url
        self._capabilities = Capabilities(downloads=False)

    @property
    def key(self) -> str:
        return KEY

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        """Look up the identifier hints of *query*.

        Raises:
            ProviderBlocked: A lookup failed or the breaker is open.
            InvalidResponse: A lookup answered something other than JSON.
        """
        hints = parse_identifiers(query.title)
        for id_type, values in parse_identifiers(query.creator).items():
            hints.setdefault(id_type, []).extend(values)
        if not hints:
            logger.info("%s: no identifier hint in the query; no search", KEY)
            return []
        results: list[SearchRecord] = []
        for id_type in ID_ORDER:
            for value in hints.get(id_type, []):
                if len(results) >= limit:
                    return results[:limit]
                if id_type == "htid":
                    results.append(_htid_record(query, value))
                    continue
                data = self._lookup(network, id_type, value)
                if data is not None:
                    results.extend(parse_bib_response(data, limit))
        return results[:limit]

    def _lookup(
        self, network: Network, id_type: str, value: str
    ) -> Mapping[str, Any] | None:
        """Return the answer for one identifier; None when it is unknown."""
        url = bib_url(id_type, value, self._bib_base_url)
        try:
            response = network.request(KEY, "GET", url)
        except RequestFailed as exc:
            if exc.cause is Cause.NOT_FOUND:
                logger.info("%s: no record for %s:%s", KEY, id_type, value)
                return None
            raise ProviderBlocked(KEY, exc.cause.value, detail=str(exc)) from exc
        except BreakerOpen as exc:
            raise provider_error(KEY, exc) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise InvalidResponse(KEY, url, "not JSON") from exc
        if not isinstance(data, Mapping):
            raise InvalidResponse(KEY, url, "not a JSON object")
        return data

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        """Raise :class:`UnsupportedCapability`: HathiTrust is metadata-only."""
        raise UnsupportedCapability(KEY, "download")

    def pages(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        first_n: int,
        max_px: int,
    ) -> list[PageRef]:
        """Raise :class:`UnsupportedCapability`: HathiTrust is metadata-only."""
        raise UnsupportedCapability(KEY, "pages")

    def item_id_from_url(self, url: str) -> str | None:
        """Return the htid of a reader or manifest URL (``babel.../cgi/pt?id=``,
        ``.../cgi/imgsrv/manifest/<htid>``) or the record number of a
        catalog URL (``catalog.hathitrust.org/Record/<n>``)."""
        if not on_hosts(url, self.hosts):
            return None
        return (
            path_match(url, _MANIFEST_HTID)
            or path_match(url, _CATALOG_RECORD)
            or query_value(url, "id")
        )


def _htid_record(query: SearchQuery, htid: str) -> SearchRecord:
    return SearchRecord(
        provider=KEY,
        source_id=htid,
        title=query.title,
        creators=(query.creator,) if query.creator else (),
        raw=MappingProxyType({"htid": htid}),
    )


def build(settings: Settings) -> HathiTrust:
    """Build the adapter; it takes nothing from settings."""
    return HathiTrust()
