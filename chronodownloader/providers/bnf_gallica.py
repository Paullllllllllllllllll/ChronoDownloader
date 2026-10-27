"""BnF Gallica: SRU search (Dublin Core), IIIF downloads.

Ported from ChronoDownloader v1.30.3 ``api/providers/bnf_gallica.py`` on
19.10.2026. Changed: an HTTP 403 from the SRU endpoint or the manifest
(Gallica's bot challenge) is reported as blocked instead of as no result,
and an answer that is not XML raises :class:`InvalidResponse`.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final

from ._query import escape_sru_literal
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

__all__ = ["BnfGallica", "build"]

logger = logging.getLogger(__name__)

KEY: Final = "bnf_gallica"
SRU_URL = "https://gallica.bnf.fr/SRU"
MANIFEST_URL = "https://gallica.bnf.fr/iiif/ark:/12148/{ark_id}/manifest.json"
ITEM_URL = "https://gallica.bnf.fr/ark:/12148/{ark_id}"

_NS = {
    "sru": "http://www.loc.gov/zing/srw/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "oai_dc": "http://www.openarchives.org/OAI/2.0/oai_dc/",
}
# The ark name stops at the first character outside its alphabet:
# dc:identifier may embed the ark in prose, a query string or a Gallica
# dot-qualifier (".texteBrut").
_ARK = re.compile(r"ark:/12148/([a-zA-Z0-9]+)")


def build_query(title: str, creator: str | None) -> str:
    """Return the CQL query for *title* and, when given, *creator*.

    The title goes to ``dc.title``: ``gallica all`` searches the full text as
    well, and its first records for "Manuel de cuisinier" were a calligraphy
    manual and other books that only mention the words.
    """
    query = f'dc.title all "{escape_sru_literal(title)}"'
    if creator:
        query += f' and dc.creator all "{escape_sru_literal(creator)}"'
    return query


def _text(element: ET.Element | None) -> str | None:
    if element is None or not element.text or not element.text.strip():
        return None
    return element.text.strip()


def _ark_id(record: ET.Element) -> str | None:
    for identifier in record.findall("dc:identifier", _NS):
        if identifier.text and "ark:/" in identifier.text:
            match = _ARK.search(identifier.text)
            if match:
                return match.group(1)
    return None


def parse_search(content: bytes, url: str = SRU_URL) -> list[SearchRecord]:
    """Return the records of an SRU answer; records without an ark are skipped.

    Every ``dc:creator`` is kept: the creator score takes the best entry, so
    dropping co-authors under-scores a query that names one of them.

    Raises:
        InvalidResponse: *content* is not XML.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise InvalidResponse(KEY, url, f"not XML: {exc}") from exc
    records: list[SearchRecord] = []
    for record in root.findall(".//sru:recordData/oai_dc:dc", _NS):
        ark_id = _ark_id(record)
        if not ark_id:
            continue
        title = record.find("dc:title", _NS)
        creators = (_text(el) for el in record.findall("dc:creator", _NS))
        records.append(
            SearchRecord(
                provider=KEY,
                source_id=ark_id,
                title=(title.text or "") if title is not None else "",
                creators=tuple(name for name in creators if name),
                date=_text(record.find("dc:date", _NS)),
                item_url=ITEM_URL.format(ark_id=ark_id),
            )
        )
    return records


def _manifest_url(record_or_id: SearchRecord | str) -> tuple[str, str]:
    """Return the item's ark name and manifest URL.

    Raises:
        ItemNotFound: The item has no ark name.
    """
    ark_id = source_id_of(record_or_id)
    if not ark_id:
        raise ItemNotFound(KEY, ark_id)
    return ark_id, MANIFEST_URL.format(ark_id=ark_id)


class BnfGallica:
    """The Gallica adapter; ``source_id`` is the ark name (``bpt6k...``).

    ``search`` lets :class:`~chronodownloader.net.RequestFailed` (cause
    ``blocked`` for Gallica's 403), :class:`~chronodownloader.net.BreakerOpen`
    and :class:`~chronodownloader.net.QuotaSpent` propagate; ``download`` and
    ``pages`` raise provider errors (a 403 as
    :class:`~chronodownloader.providers.base.ProviderBlocked`).
    """

    key: Final = KEY
    capabilities: Final = Capabilities(iiif=True)
    hosts: ClassVar[frozenset[str]] = frozenset({"gallica.bnf.fr"})

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        """Return at most *limit* records that carry an ark."""
        logger.info("Searching Gallica for: %s", query.title)
        response = network.request(
            KEY,
            "GET",
            SRU_URL,
            params={
                "version": "1.2",
                "operation": "searchRetrieve",
                "query": build_query(query.title, query.creator),
                "maximumRecords": str(limit),
                "recordSchema": "oai_dc",
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
        ark_id, url = _manifest_url(record_or_id)
        manifest = fetch_json(network, KEY, url, slot=request.slot, item=ark_id)
        return download_manifest(
            network,
            KEY,
            manifest,
            source_id=ark_id,
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
        ark_id, url = _manifest_url(record_or_id)
        manifest = fetch_json(network, KEY, url, item=ark_id)
        return manifest_page_refs(manifest, first_n=first_n, max_px=max_px)

    def item_id_from_url(self, url: str) -> str | None:
        """Return the ark name of an item URL or of the IIIF manifest.

        Item URLs read ``/ark:/12148/bpt6k...``, possibly with ``.image`` or
        ``/f1.item`` after it.
        """
        if not on_hosts(url, self.hosts):
            return None
        return path_match(url, _ARK)

    def item_manifest_url(self, item_id: str) -> str:
        """Return the item's IIIF manifest URL."""
        return MANIFEST_URL.format(ark_id=item_id)


def build(settings: Settings) -> Provider:
    """Return the Gallica adapter; it reads no settings."""
    return BnfGallica()
