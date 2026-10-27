"""Library of Congress: loc.gov JSON search, downloads through IIIF.

Ported from ChronoDownloader v1.30.3 ``api/providers/loc.py`` on 19.10.2026.
Kept: the Books collection searched first and the general search when it has
no results, the record fields (last path segment of ``id``, first of
``contributor_names``, ``date`` else the first of ``dates``), the item JSON
fetched with ``fo=json`` joined by the right separator, the manifest taken
from the candidate or the item's ``resources``, and the fallback to the
item's representative image (the last, largest entry of ``image_url``).
Changed: a failed request raises instead of returning no results (search and
manifests answered HTTP 403 in the 19.10.2026 baseline); the representative
image is staged as a preview that never completes the work; a bare id is
looked up at ``https://www.loc.gov/item/<id>/``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar

from chronodownloader.coord import SlotToken
from chronodownloader.net import Network
from chronodownloader.settings import Settings

from ._query import parse_year
from ._support import Collector, as_creators, fetch_document, has_content
from ._urls import on_hosts, path_match
from .base import (
    Capabilities,
    Completeness,
    DownloadRequest,
    DownloadResult,
    InvalidResponse,
    ItemNotFound,
    PageRef,
    SearchQuery,
    SearchRecord,
    source_id_of,
)
from .iiif_download import download_manifest, fetch_json, manifest_page_refs

__all__ = [
    "KEY",
    "LOC_API_BASE_URL",
    "LocProvider",
    "build",
    "image_url_of",
    "item_json_url",
    "manifest_from_item",
    "parse_result",
]

KEY = "loc"
LOC_API_BASE_URL = "https://www.loc.gov/"
_ACCEPT_JSON = {"Accept": "application/json"}
_ITEM_PATH = re.compile(r"^/item/([^/?#]+)")


def item_json_url(item_url: str) -> str:
    """Return the item URL asking for JSON (``fo=json``).

    A second ``?`` on a URL that already carries a query would make LoC
    ignore the parameter and serve HTML.
    """
    if "fo=json" in item_url:
        return item_url
    separator = "&" if "?" in item_url else "?"
    return f"{item_url}{separator}fo=json"


def _resources_manifest(resources: Any) -> str | None:
    if isinstance(resources, Mapping):
        resources = [resources]
    for resource in resources if isinstance(resources, list) else []:
        if isinstance(resource, Mapping):
            manifest = resource.get("iiif_manifest")
            if isinstance(manifest, str) and manifest:
                return manifest
    return None


def _result_manifest(item: Mapping[str, Any]) -> str | None:
    manifest = item.get("iiif_manifest_url")
    if isinstance(manifest, str) and manifest:
        return manifest
    return _resources_manifest(item.get("resources"))


def _first_name(contributors: Any) -> str | None:
    # A bare string must not be indexed (that took its first character).
    if isinstance(contributors, list) and contributors:
        return str(contributors[0])
    if isinstance(contributors, str) and contributors:
        return contributors
    return None


def _date(item: Mapping[str, Any]) -> str | None:
    value = item.get("date")
    if not value:
        dates = item.get("dates")
        if isinstance(dates, list) and dates:
            value = dates[0]
        elif isinstance(dates, str):
            value = dates
    return str(value) if value else None


def parse_result(item: Any) -> SearchRecord | None:
    """Return the record of one search result, or None without an id."""
    if not isinstance(item, Mapping):
        return None
    raw_id = item.get("id")
    if not isinstance(raw_id, str) or not raw_id.strip("/"):
        return None
    item_id = raw_id.strip("/").split("/")[-1]
    creator = _first_name(item.get("contributor_names"))
    date = _date(item)
    url = item.get("url") if isinstance(item.get("url"), str) else None
    manifest = _result_manifest(item)
    title = str(item.get("title") or "")
    raw = {
        "title": title,
        "creator": creator,
        "date": date,
        "id": item_id,
        "item_url": url,
        "iiif_manifest": manifest,
    }
    return SearchRecord(
        provider=KEY,
        source_id=item_id,
        title=title,
        creators=as_creators(creator),
        date=date,
        year=parse_year(date),
        item_url=url,
        iiif_manifest=manifest,
        raw=MappingProxyType(raw),
    )


def _results(payload: Any) -> list[Any]:
    """Return a search answer's results (top level or under ``content``)."""
    if not isinstance(payload, Mapping):
        return []
    results = payload.get("results")
    if not results:
        # "content" is not always an object: some answers carry a string.
        content = payload.get("content")
        results = content.get("results") if isinstance(content, Mapping) else None
    return results if isinstance(results, list) else []


def manifest_from_item(details: Mapping[str, Any]) -> str | None:
    """Return the manifest in an item JSON's ``item.resources`` or ``resources``.

    The envelope varies: ``item`` may be a URL string and ``resources`` a
    string; both are ignored then.
    """
    item = details.get("item")
    if isinstance(item, Mapping):
        found = _resources_manifest(item.get("resources"))
        if found:
            return found
    return _resources_manifest(details.get("resources"))


def image_url_of(details: Mapping[str, Any]) -> str | None:
    """Return the item's representative image URL, absolute.

    ``image_url`` is most often a list ordered by increasing resolution; the
    last entry is taken. A dict entry gives its ``medium`` or ``full`` URL.
    """
    item = details.get("item")
    value = item.get("image_url") if isinstance(item, Mapping) else None
    if isinstance(value, list):
        value = value[-1] if value else None
    if isinstance(value, Mapping):
        value = value.get("medium") or value.get("full")
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("//"):
        return f"https:{value}"
    if not value.startswith("http"):
        return f"https://www.loc.gov{value}"
    return value


class LocProvider:
    """The Library of Congress adapter.

    ``source_id`` is the item id: the last segment of its ``/item/<id>/``
    URL, such as an LCCN.
    """

    hosts: ClassVar[frozenset[str]] = frozenset({"loc.gov"})

    @property
    def key(self) -> str:
        """The provider key."""
        return KEY

    @property
    def capabilities(self) -> Capabilities:
        """What the adapter offers."""
        return Capabilities(iiif=True)

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        """Return at most *limit* records for the title and creator.

        Raises:
            ProviderBlocked: A request failed.
            InvalidResponse: An answer is not JSON.
        """
        terms = " ".join(part for part in (query.title, query.creator) if part)
        params = {"q": terms, "fo": "json", "c": str(limit)}
        results: list[Any] = []
        for path in ("books/", "search/"):
            data = fetch_document(
                network,
                KEY,
                f"{LOC_API_BASE_URL}{path}",
                params=params,
                headers=_ACCEPT_JSON,
            )
            results = _results(data)
            if results:
                break
        records = [parse_result(item) for item in results]
        return [record for record in records if record is not None][:limit]

    @staticmethod
    def _item_url(record_or_id: SearchRecord | str) -> str:
        """Return the item page URL.

        Raises:
            ItemNotFound: A record without an item URL.
        """
        if isinstance(record_or_id, SearchRecord):
            url = record_or_id.item_url or record_or_id.raw.get("item_url")
            if not isinstance(url, str) or not url:
                raise ItemNotFound(KEY, record_or_id.source_id)
            return url
        if not record_or_id.strip("/"):
            raise ItemNotFound(KEY, record_or_id)
        return f"{LOC_API_BASE_URL}item/{record_or_id.strip('/')}/"

    def _details(
        self,
        network: Network,
        record_or_id: SearchRecord | str,
        slot: SlotToken | None,
    ) -> tuple[str, Mapping[str, Any]]:
        """Return the item JSON's URL and answer.

        Raises:
            ItemNotFound: No item URL, or the item is unknown.
            ProviderBlocked: The request failed.
            InvalidResponse: The answer is not a JSON object.
        """
        url = item_json_url(self._item_url(record_or_id))
        details = fetch_document(
            network,
            KEY,
            url,
            headers=_ACCEPT_JSON,
            slot=slot,
            item=source_id_of(record_or_id),
        )
        if not isinstance(details, Mapping):
            raise InvalidResponse(KEY, url, "not a JSON object")
        return url, details

    @staticmethod
    def _manifest_url(
        record_or_id: SearchRecord | str, details: Mapping[str, Any]
    ) -> str | None:
        if isinstance(record_or_id, SearchRecord) and record_or_id.iiif_manifest:
            return record_or_id.iiif_manifest
        return manifest_from_item(details)

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        """Stage the item JSON and the manifest's files; else a preview.

        Raises:
            ItemNotFound: The item or its manifest is unknown.
            ProviderBlocked: The item or manifest request failed.
            InvalidResponse: The item or manifest is not a JSON object.
            OSError: A file could not be written.
        """
        source_id = source_id_of(record_or_id)
        url, details = self._details(network, record_or_id, request.slot)
        collector = Collector(network, KEY, staging_dir, request)
        collector.document("item", details, source_url=url)
        manifest_url = self._manifest_url(record_or_id, details)
        parts: tuple[Completeness, ...] = ()
        if manifest_url is not None:
            manifest = fetch_json(
                network, KEY, manifest_url, slot=request.slot, item=source_id
            )
            result = download_manifest(
                network,
                KEY,
                manifest,
                source_id=source_id,
                staging_dir=staging_dir,
                request=request,
                manifest_url=manifest_url,
            )
            collector.absorb(result)
            parts = result.parts
            if has_content(result) or result.stopped is not None:
                return collector.result(source_id, parts)
        collector.preview([image_url_of(details)])
        return collector.result(source_id, parts)

    def pages(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        first_n: int,
        max_px: int,
    ) -> list[PageRef]:
        """Return the manifest's first canvases; none without a manifest.

        A candidate carrying its manifest URL needs no item request.

        Raises:
            ItemNotFound, ProviderBlocked, InvalidResponse: As ``download``.
        """
        source_id = source_id_of(record_or_id)
        if isinstance(record_or_id, SearchRecord) and record_or_id.iiif_manifest:
            manifest_url: str | None = record_or_id.iiif_manifest
        else:
            _url, details = self._details(network, record_or_id, None)
            manifest_url = manifest_from_item(details)
        if manifest_url is None:
            return []
        manifest = fetch_json(network, KEY, manifest_url, item=source_id)
        return manifest_page_refs(manifest, first_n=first_n, max_px=max_px)

    def item_id_from_url(self, url: str) -> str | None:
        """Return the id of an item URL (``/item/<id>/``, its manifest too)."""
        if not on_hosts(url, self.hosts):
            return None
        return path_match(url, _ITEM_PATH)

    def item_manifest_url(self, item_id: str) -> str:
        """Return the item's IIIF manifest URL."""
        return f"{LOC_API_BASE_URL}item/{item_id.strip('/')}/manifest.json"


def build(settings: Settings) -> LocProvider:
    """Return the adapter (LoC needs no key)."""
    return LocProvider()
