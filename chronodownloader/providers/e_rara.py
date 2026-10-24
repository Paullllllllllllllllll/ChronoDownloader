"""e-rara.ch: SRU search (MODS), IIIF downloads by Visual Library id.

Ported from ChronoDownloader v1.30.3 ``api/providers/e_rara.py`` on
19.10.2026. Changed: an answer that is not XML raises
:class:`InvalidResponse` instead of yielding no results.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final

from ._query import escape_sru_literal, mods_creator, mods_date, parse_year
from ._urls import on_hosts, path_match
from .base import (
    Capabilities,
    DownloadRequest,
    DownloadResult,
    InvalidResponse,
    ItemNotFound,
    PageRef,
    Provider,
    SearchQuery,
    SearchRecord,
    source_id_of,
)
from .iiif_download import download_manifest, fetch_json, manifest_page_refs

if TYPE_CHECKING:
    from chronodownloader.net import Network
    from chronodownloader.settings import Settings

__all__ = ["ERara", "build"]

logger = logging.getLogger(__name__)

KEY: Final = "e_rara"
SRU_URL = "https://www.e-rara.ch/sru"
MANIFEST_URL = "https://www.e-rara.ch/i3f/v20/{vlid}/manifest"
ITEM_URL = "https://www.e-rara.ch/{prefix}/{vlid}"

_TITLE_PATH = re.compile(r"/(?:content/(?:titleinfo|structure)|i3f/v\d+)/(\d+)")

_NS = {
    "srw": "http://www.loc.gov/zing/srw/",
    "mods": "http://www.loc.gov/mods/v3",
    "vl": "http://visuallibrary.net/vl",
}


def build_query(title: str, creator: str | None) -> str:
    """Return the CQL query: quoted terms joined by ``and``.

    CQL needs an explicit boolean between terms; a bare space is invalid.
    """
    terms = [f'"{escape_sru_literal(term)}"' for term in (title, creator) if term]
    return " and ".join(terms)


def _child_text(parent: ET.Element | None, path: str) -> str | None:
    element = parent.find(path, _NS) if parent is not None else None
    if element is None or not element.text or not element.text.strip():
        return None
    return element.text.strip()


def _record(record: ET.Element) -> SearchRecord | None:
    """Return the record of one SRU entry, or None without MODS or an id."""
    mods = record.find(".//mods:mods", _NS)
    extra = record.find(".//srw:extraRecordData", _NS)
    vlid = _child_text(extra, "vl:id")
    if mods is None or not vlid:
        return None
    prefix = _child_text(extra, "vl:prefix")
    # A top-level name only: ".//mods:name" would also match a subject
    # heading, the person the work is about.
    creator = mods_creator(mods, _NS)
    creators = [part.strip() for part in (creator or "").split(";")]
    date = mods_date(mods, _NS)
    return SearchRecord(
        provider=KEY,
        source_id=vlid,
        title=_child_text(mods, ".//mods:titleInfo/mods:title") or "",
        creators=tuple(part for part in creators if part),
        date=date,
        year=parse_year(date),
        item_url=ITEM_URL.format(prefix=prefix, vlid=vlid) if prefix else None,
        iiif_manifest=MANIFEST_URL.format(vlid=vlid),
    )


def parse_search(content: bytes, url: str = SRU_URL) -> list[SearchRecord]:
    """Return the records of an SRU answer.

    Raises:
        InvalidResponse: *content* is not XML.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise InvalidResponse(KEY, url, f"not XML: {exc}") from exc
    records = (_record(record) for record in root.findall(".//srw:record", _NS))
    return [record for record in records if record is not None]


def _manifest_url(record_or_id: SearchRecord | str) -> tuple[str, str]:
    """Return the item's id and manifest URL; a record's own manifest wins.

    Raises:
        ItemNotFound: The item has neither an id nor a manifest.
    """
    vlid = source_id_of(record_or_id)
    if isinstance(record_or_id, SearchRecord) and record_or_id.iiif_manifest:
        return vlid, record_or_id.iiif_manifest
    if not vlid:
        raise ItemNotFound(KEY, vlid)
    return vlid, MANIFEST_URL.format(vlid=vlid)


class ERara:
    """The e-rara adapter; ``source_id`` is the Visual Library id (VLID).

    ``search`` lets :class:`~chronodownloader.net.RequestFailed`,
    :class:`~chronodownloader.net.BreakerOpen` and
    :class:`~chronodownloader.net.QuotaSpent` propagate; ``download`` and
    ``pages`` raise provider errors.
    """

    key: Final = KEY
    capabilities: Final = Capabilities(iiif=True)
    hosts: ClassVar[frozenset[str]] = frozenset({"e-rara.ch"})

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        """Return at most *limit* records; an empty query sends nothing."""
        cql = build_query(query.title, query.creator)
        if not cql:
            return []
        logger.info("Searching e-rara for: %s", query.title)
        response = network.request(
            KEY,
            "GET",
            SRU_URL,
            params={
                "version": "1.2",
                "operation": "searchRetrieve",
                "query": cql,
                "maximumRecords": str(limit),
                "recordSchema": "mods",
            },
        )
        return parse_search(response.content)[:limit]

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        """Stage the item's manifest, rendering and page images."""
        vlid, url = _manifest_url(record_or_id)
        manifest = fetch_json(network, KEY, url, slot=request.slot, item=vlid)
        return download_manifest(
            network,
            KEY,
            manifest,
            source_id=vlid,
            staging_dir=staging_dir,
            request=request,
            manifest_url=url,
            image_max_px=self.capabilities.image_max_px,
        )

    def pages(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        first_n: int,
        max_px: int,
    ) -> list[PageRef]:
        """Return the first *first_n* canvases of the item at *max_px*."""
        vlid, url = _manifest_url(record_or_id)
        manifest = fetch_json(network, KEY, url, item=vlid)
        return manifest_page_refs(manifest, first_n=first_n, max_px=max_px)

    def item_id_from_url(self, url: str) -> str | None:
        """Return the VLID of a title URL (``/<prefix>/content/titleinfo/<id>``,
        ``.../structure/<id>``) or of the IIIF manifest (``/i3f/v20/<id>/``).

        A ``pageview`` or ``zoom`` URL names a page, not the title: None.
        """
        if not on_hosts(url, self.hosts):
            return None
        return path_match(url, _TITLE_PATH)

    def item_manifest_url(self, item_id: str) -> str:
        """Return the item's IIIF manifest URL."""
        return MANIFEST_URL.format(vlid=item_id)


def build(settings: Settings) -> Provider:
    """Return the e-rara adapter; it reads no settings."""
    return ERara()
