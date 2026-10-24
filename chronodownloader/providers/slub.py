"""SLUB Dresden: LOD search (data.slub-dresden.de), IIIF downloads by PPN.

Ported from ChronoDownloader v1.30.3 ``api/providers/slub.py`` on
19.10.2026. Search asks the LOD API for creative works that are online; a
download reads the catalogue source record, takes the PPN from its first
856 link and fetches the manifest of the digital collections. Changed: the
source record is staged as ``metadata/source.json``; a record without a PPN
raises :class:`ItemNotFound` and an answer that is not JSON raises
:class:`InvalidResponse` instead of yielding no results.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final

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
    StagedFile,
    source_id_of,
)
from .iiif_download import download_manifest, fetch_json, manifest_page_refs
from .staging import stage_metadata

if TYPE_CHECKING:
    import requests

    from chronodownloader.coord import SlotToken
    from chronodownloader.net import Network
    from chronodownloader.settings import Settings

__all__ = ["Slub", "build"]

logger = logging.getLogger(__name__)

KEY: Final = "slub"
SEARCH_URL = "https://data.slub-dresden.de/search"
SOURCE_URL = "https://data.slub-dresden.de/source/kxp-de14/{record_id}"
MANIFEST_URL = "https://iiif.slub-dresden.de/iiif/2/{ppn}/manifest.json"
SOURCE_NAME = "source"
"""The staged source record's metadata name (``metadata/source.json``)."""

# K10plus PPNs end in a modulo-11 check digit that is "X" for one number in
# eleven; "\d+" alone truncated it, and the manifest of the short PPN 404s.
_PPN_PATTERNS = (
    re.compile(r"ppn(\d+[\dxX]?)", re.IGNORECASE),
    re.compile(r"id(\d+[\dxX]?)", re.IGNORECASE),
)
_MANIFEST_PPN = re.compile(r"^/iiif/\d+/(\d+[\dxX]?)/manifest")


def extract_ppn(url: str | None) -> str | None:
    """Return the PPN of a digital collections URL (``.../id<PPN>``), or None."""
    if not url:
        return None
    for pattern in _PPN_PATTERNS:
        match = pattern.search(url)
        if match:
            return match.group(1)
    return None


def _title(item: Mapping[str, Any]) -> str:
    title = item.get("title")
    if isinstance(title, Mapping):
        found = title.get("mainTitle") or title.get("preferredName")
        if found:
            return str(found)
    elif isinstance(title, str):
        return title
    return str(item.get("preferredName") or "")


def _creator(item: Mapping[str, Any]) -> str | None:
    contributors = item.get("contributor")
    if isinstance(contributors, list):
        for contributor in contributors:
            if isinstance(contributor, Mapping) and contributor.get("name"):
                return str(contributor["name"])
    return None


def _record_id(item: Mapping[str, Any]) -> str | None:
    record_id = item.get("@id") or item.get("id")
    if record_id and isinstance(record_id, str):
        return record_id.rstrip("/").split("/")[-1]
    return None


def _is_online(item: Mapping[str, Any]) -> bool:
    access_mode = str(item.get("accessMode") or "").lower()
    reproduction = str(item.get("reproductionType") or "").lower()
    return not access_mode or access_mode == "online" or "online" in reproduction


def _record(item: Any) -> SearchRecord | None:
    """Return the record of one search item; None when offline or without id."""
    if not isinstance(item, Mapping) or not _is_online(item):
        return None
    record_id = _record_id(item)
    if not record_id:
        return None
    creator = _creator(item) or ""
    creators = (part.strip() for part in creator.split(";"))
    return SearchRecord(
        provider=KEY,
        source_id=record_id,
        title=_title(item),
        creators=tuple(part for part in creators if part),
    )


def parse_search(response: requests.Response) -> list[SearchRecord]:
    """Return the online records of a search answer.

    Raises:
        InvalidResponse: The answer is not a JSON list.
    """
    try:
        data = response.json()
    except ValueError as exc:
        raise InvalidResponse(KEY, response.url, "not JSON") from exc
    if not isinstance(data, list):
        raise InvalidResponse(KEY, response.url, "not a JSON list")
    records = (_record(item) for item in data)
    return [record for record in records if record is not None]


def digital_url(source: Mapping[str, Any]) -> str | None:
    """Return the first link (``u``) of the source record's 856 fields."""
    for entry in source.get("856") or []:
        if not isinstance(entry, Mapping):
            continue
        for subfields in entry.values():
            if not isinstance(subfields, list):
                continue
            for subfield in subfields:
                if isinstance(subfield, Mapping) and subfield.get("u"):
                    return str(subfield["u"])
    return None


class Slub:
    """The SLUB adapter; ``source_id`` is the K10plus record id.

    The record id names the catalogue record, the PPN in its 856 link the
    digitized copy whose manifest is fetched; ``source_id`` stays the record
    id, as in v1.30.3. ``search`` lets
    :class:`~chronodownloader.net.RequestFailed`,
    :class:`~chronodownloader.net.BreakerOpen` and
    :class:`~chronodownloader.net.QuotaSpent` propagate; ``download`` and
    ``pages`` raise provider errors.
    """

    key: Final = KEY
    capabilities: Final = Capabilities(iiif=True)
    hosts: ClassVar[frozenset[str]] = frozenset({"slub-dresden.de"})

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        """Return at most *limit* records of online creative works."""
        terms = f"{query.title} {query.creator}" if query.creator else query.title
        logger.info("Searching SLUB Dresden for: %s", query.title)
        response = network.request(
            KEY,
            "GET",
            SEARCH_URL,
            params={
                "q": escape_sru_literal(terms),
                "size": str(limit),
                "format": "json",
                "filter": "@type:http://schema.org/CreativeWork",
            },
        )
        return parse_search(response)[:limit]

    def _manifest_url(
        self,
        network: Network,
        record_or_id: SearchRecord | str,
        slot: SlotToken | None,
    ) -> tuple[str, Mapping[str, Any] | None]:
        """Return the manifest URL and the source record it came from.

        A record's own manifest wins and needs no source record.

        Raises:
            ItemNotFound: The record is unknown or its source names no PPN.
            ProviderBlocked: The source request failed otherwise.
            InvalidResponse: The source record is not a JSON object.
        """
        if isinstance(record_or_id, SearchRecord) and record_or_id.iiif_manifest:
            return record_or_id.iiif_manifest, None
        record_id = source_id_of(record_or_id)
        if not record_id:
            raise ItemNotFound(KEY, record_id)
        url = SOURCE_URL.format(record_id=record_id)
        source = fetch_json(network, KEY, url, slot=slot, item=record_id)
        ppn = extract_ppn(digital_url(source))
        if not ppn:
            raise ItemNotFound(KEY, record_id, url)
        return MANIFEST_URL.format(ppn=ppn), source

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        """Stage the source record, the manifest and the item's files."""
        record_id = source_id_of(record_or_id)
        url, source = self._manifest_url(network, record_or_id, request.slot)
        manifest = fetch_json(network, KEY, url, slot=request.slot, item=record_id)
        staged_source = _stage_source(staging_dir, source, record_id, request)
        result = download_manifest(
            network,
            KEY,
            manifest,
            source_id=record_id,
            staging_dir=staging_dir,
            request=request,
            manifest_url=url,
            image_max_px=self.capabilities.image_max_px,
        )
        if staged_source is None:
            return result
        return replace(result, files=(staged_source, *result.files))

    def pages(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        first_n: int,
        max_px: int,
    ) -> list[PageRef]:
        """Return the first *first_n* canvases of the item at *max_px*."""
        url, _source = self._manifest_url(network, record_or_id, None)
        manifest = fetch_json(network, KEY, url, item=source_id_of(record_or_id))
        return manifest_page_refs(manifest, first_n=first_n, max_px=max_px)

    def item_id_from_url(self, url: str) -> str | None:
        """Return the PPN of a digital collections URL (``/id<PPN>``, a
        ``PPN`` parameter) or of the IIIF manifest (``/iiif/2/<PPN>/...``).

        A ``werkansicht/dlf/<n>`` URL names the viewer's internal number,
        not the PPN: None.
        """
        if not on_hosts(url, self.hosts):
            return None
        return path_match(url, _MANIFEST_PPN) or extract_ppn(url)

    def item_manifest_url(self, item_id: str) -> str:
        """Return the manifest URL of the digitized copy with PPN *item_id*."""
        return MANIFEST_URL.format(ppn=item_id)


def _stage_source(
    staging_dir: Path,
    source: Mapping[str, Any] | None,
    record_id: str,
    request: DownloadRequest,
) -> StagedFile | None:
    """Stage the source record as metadata when the request and budget allow."""
    budget = request.budget
    if source is None or not request.include_metadata:
        return None
    if budget is not None and not budget.allows("metadata"):
        return None
    staged = stage_metadata(
        staging_dir,
        SOURCE_NAME,
        source,
        source_url=SOURCE_URL.format(record_id=record_id),
    )
    if budget is not None:
        budget.charge(staged.kind, staged.size)
    return staged


def build(settings: Settings) -> Provider:
    """Return the SLUB adapter; it reads no settings."""
    return Slub()
