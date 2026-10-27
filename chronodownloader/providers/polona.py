"""Polona.pl: JSON search through the search-service gateway, IIIF downloads.

Ported from ChronoDownloader v1.30.3 ``api/providers/polona.py`` on
19.10.2026. Polona is a single-page application; its Spring gateway at
``https://polona.pl/api`` answers searches and serves the manifests. Pages
answer HTTP 400 at ``full/full`` and are fetched at the next size candidate
(``full/max``), which the shared IIIF download tries after a 400. Changed:
an answer that is not a JSON object raises :class:`InvalidResponse` instead
of yielding no results.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final

from ._query import parse_year
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
    import requests

    from chronodownloader.net import Network
    from chronodownloader.settings import Settings

__all__ = ["Polona", "build"]

logger = logging.getLogger(__name__)

KEY: Final = "polona"
API_BASE_URL = "https://polona.pl/api"
SEARCH_URL = f"{API_BASE_URL}/search-service/search/simple"
MANIFEST_URL = f"{API_BASE_URL}/search-service/search/iiif/{{item_id}}/manifest.json"
ITEM_URL = "https://polona.pl/preview/{item_id}"

_OBJECT_PATH = re.compile(
    r"/(?:preview|item-view|item|search/iiif)/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:[/?#]|$)",
    re.IGNORECASE,
)


def first_value(fields: Any, name: str) -> str | None:
    """Return the first non-empty value of a Polona metadata field.

    Polona wraps every field as ``{"name": ..., "values": [...], ...}``
    inside ``basicFields``, ``expandedFields`` or ``hiddenFields``.
    """
    field = fields.get(name) if isinstance(fields, Mapping) else None
    values = field.get("values") if isinstance(field, Mapping) else None
    if isinstance(values, list):
        for value in values:
            if value:
                return str(value)
    return None


def thumbnail_url(attributes: Any) -> str | None:
    """Return a hit's thumbnail URL, made absolute against the gateway."""
    thumbnail = attributes.get("thumbnail") if isinstance(attributes, Mapping) else None
    value = thumbnail.get("stringValue") if isinstance(thumbnail, Mapping) else None
    if not value:
        return None
    text = str(value)
    return f"{API_BASE_URL}{text}" if text.startswith("/") else text


def _record(hit: Any) -> SearchRecord | None:
    """Return the record of one hit, or None without an object id."""
    if not isinstance(hit, Mapping) or not hit.get("objectId"):
        return None
    object_id = str(hit["objectId"])
    basic = hit.get("basicFields")
    creator = first_value(basic, "creatorForSearch") or ""
    creators = (part.strip() for part in creator.split(";"))
    thumbnail = thumbnail_url(hit.get("attributes"))
    date = first_value(basic, "dateDescriptive") or first_value(
        hit.get("hiddenFields"), "date"
    )
    return SearchRecord(
        provider=KEY,
        source_id=object_id,
        title=first_value(basic, "title") or "",
        creators=tuple(part for part in creators if part),
        date=date,
        year=parse_year(date),
        item_url=ITEM_URL.format(item_id=object_id),
        raw={"thumbnail": thumbnail} if thumbnail else {},
    )


def parse_search(response: requests.Response) -> list[SearchRecord]:
    """Return the records of a search answer; the thumbnail goes to ``raw``.

    Raises:
        InvalidResponse: The answer is not a JSON object.
    """
    try:
        data = response.json()
    except ValueError as exc:
        raise InvalidResponse(KEY, response.url, "not JSON") from exc
    if not isinstance(data, Mapping):
        raise InvalidResponse(KEY, response.url, "not a JSON object")
    records = (_record(hit) for hit in data.get("hits") or [])
    return [record for record in records if record is not None]


def _manifest_url(record_or_id: SearchRecord | str) -> tuple[str, str]:
    """Return the item's object id and manifest URL.

    Raises:
        ItemNotFound: The item has no object id.
    """
    item_id = source_id_of(record_or_id)
    if not item_id:
        raise ItemNotFound(KEY, item_id)
    return item_id, MANIFEST_URL.format(item_id=item_id)


class Polona:
    """The Polona adapter; ``source_id`` is the object id (a UUID).

    ``search`` lets :class:`~chronodownloader.net.RequestFailed`,
    :class:`~chronodownloader.net.BreakerOpen` and
    :class:`~chronodownloader.net.QuotaSpent` propagate; ``download`` and
    ``pages`` raise provider errors.
    """

    key: Final = KEY
    capabilities: Final = Capabilities(iiif=True)
    hosts: ClassVar[frozenset[str]] = frozenset({"polona.pl"})

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        """Return at most *limit* hits; the creator is appended to the query."""
        terms = f"{query.title} {query.creator}" if query.creator else query.title
        logger.info("Searching Polona for: %s", query.title)
        # All four parameters are mandatory; without "sort" the gateway
        # answers HTTP 400.
        response = network.request(
            KEY,
            "GET",
            SEARCH_URL,
            params={
                "query": terms,
                "page": 0,
                "pageSize": max(1, limit),
                "sort": "RELEVANCE",
            },
        )
        return parse_search(response)[:limit]

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        """Stage the item's manifest, rendering and page images."""
        item_id, url = _manifest_url(record_or_id)
        manifest = fetch_json(network, KEY, url, slot=request.slot, item=item_id)
        return download_manifest(
            network,
            KEY,
            manifest,
            source_id=item_id,
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
        item_id, url = _manifest_url(record_or_id)
        manifest = fetch_json(network, KEY, url, item=item_id)
        return manifest_page_refs(manifest, first_n=first_n, max_px=max_px)

    def item_id_from_url(self, url: str) -> str | None:
        """Return the object UUID of a viewer URL or of the manifest.

        Viewer URLs read ``/preview/<uuid>``, ``/item-view/<uuid>`` or
        ``/item/<uuid>``.
        """
        if not on_hosts(url, self.hosts):
            return None
        found = path_match(url, _OBJECT_PATH)
        return found.lower() if found else None

    def item_manifest_url(self, item_id: str) -> str:
        """Return the object's IIIF manifest URL on the gateway."""
        return MANIFEST_URL.format(item_id=item_id)


def build(settings: Settings) -> Provider:
    """Return the Polona adapter; it reads no settings."""
    return Polona()
