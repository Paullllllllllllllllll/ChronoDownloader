"""DPLA: Digital Public Library of America items, downloads through IIIF.

Ported from ChronoDownloader v1.30.3 ``api/providers/dpla.py`` on
19.10.2026. Kept: the query (``q`` with the title, ``sourceResource.creator``
with the creator), the date unwrapping, the manifest discovery (a URL naming
both ``iiif`` and ``manifest`` in ``object``, ``isShownAt``, ``isShownBy`` or
``hasView``, else the candidate's stored manifest), the item fields read off
the ``docs`` envelope of the single-item endpoint, and the fallback to
``isShownBy``, ``hasView`` and ``object`` when the manifest yields nothing.
Changed: a failed request raises instead of returning no results; a missing
key raises :class:`~.base.MissingApiKey`. A record without a manifest (most
of them: ``isShownAt`` points at an Internet Archive or HathiTrust page and
``object`` at a thumbnail) cannot be downloaded through DPLA, in v1.30.3
neither, which recorded the thumbnail as a completed download; ``download``
now raises :class:`~.base.UnsupportedCapability` naming the holding
library's page, which the run records as ``needs_review`` (``metadata_only``).
A fallback file after an empty manifest is staged as a preview and the
result reports the work missing.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar

from chronodownloader.coord import SlotToken
from chronodownloader.net import Network
from chronodownloader.net.credentials import redact_url
from chronodownloader.settings import Settings

from ._query import parse_year
from ._support import (
    Collector,
    as_creators,
    fetch_document,
    has_content,
)
from ._urls import on_hosts, path_match
from .base import (
    Capabilities,
    DownloadRequest,
    DownloadResult,
    InvalidResponse,
    ItemNotFound,
    MissingApiKey,
    PageRef,
    SearchQuery,
    SearchRecord,
    UnsupportedCapability,
    source_id_of,
)
from .iiif_download import download_manifest, fetch_json, manifest_page_refs

__all__ = [
    "API_BASE_URL",
    "KEY",
    "DplaProvider",
    "build",
    "display_date",
    "fallback_urls",
    "find_manifest",
    "item_doc",
    "parse_doc",
]

KEY = "dpla"
API_BASE_URL = "https://api.dp.la/v2/"

_LINK_KEYS = ("@id", "id", "url")
_ITEM_PATH = re.compile(r"/items?/([0-9a-f]{32})(?:[/?#]|$)")


def display_date(value: Any) -> str | None:
    """Unwrap a ``sourceResource.date``: a list, a ``displayDate`` object
    (``begin`` as fallback) or a bare string; None when empty."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, Mapping):
        value = value.get("displayDate") or value.get("begin")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _view_links(view: Any) -> list[str]:
    """Return the URLs of a ``hasView`` value (a list or one entry)."""
    links: list[str] = []
    for entry in view if isinstance(view, list) else [view]:
        if isinstance(entry, str):
            links.append(entry)
        elif isinstance(entry, Mapping):
            links.extend(
                entry[key] for key in _LINK_KEYS if isinstance(entry.get(key), str)
            )
    return links


def _is_manifest(url: str) -> bool:
    return "manifest" in url and "iiif" in url


def find_manifest(doc: Mapping[str, Any]) -> str | None:
    """Return the first IIIF manifest URL among a doc's link fields."""
    for key in ("object", "isShownAt", "isShownBy"):
        value = doc.get(key)
        if isinstance(value, str) and _is_manifest(value):
            return value
    return next(
        (url for url in _view_links(doc.get("hasView")) if _is_manifest(url)), None
    )


def item_doc(details: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the item of a single-item answer (``{"count": 1, "docs": [...]}``);
    a mapping without ``docs`` is the item itself."""
    docs = details.get("docs")
    if isinstance(docs, list) and docs and isinstance(docs[0], Mapping):
        return docs[0]
    return details


def fallback_urls(doc: Mapping[str, Any], search_raw: Mapping[str, Any]) -> list[str]:
    """Return the preview candidates: ``isShownBy``, ``hasView``, ``object``.

    Each field is read from the item, else from the candidate's search fields.
    """
    urls: list[str] = []
    for key in ("isShownBy", "hasView", "object"):
        value = doc.get(key)
        if value is None:
            value = search_raw.get(key)
        if key == "hasView":
            urls.extend(_view_links(value))
        elif isinstance(value, str):
            urls.append(value)
    return urls


def _no_manifest(doc: Mapping[str, Any]) -> str:
    """Say that the item names no manifest, and where its library shows it."""
    shown_at = doc.get("isShownAt")
    if isinstance(shown_at, str) and shown_at:
        return (
            "the record names no IIIF manifest; the item is held at "
            f"{redact_url(shown_at)} (fetch it from that library)"
        )
    return "the record names no IIIF manifest and no holding library"


def parse_doc(doc: Any) -> SearchRecord | None:
    """Return the record of one search doc, or None without an id."""
    if not isinstance(doc, Mapping):
        return None
    doc_id = doc.get("id")
    if not isinstance(doc_id, str) or not doc_id:
        return None
    source = doc.get("sourceResource")
    source = source if isinstance(source, Mapping) else {}
    title = source.get("title")
    if isinstance(title, list):
        title = title[0] if title else ""
    creators = source.get("creator") or []
    if isinstance(creators, str):
        creators = [creators]
    # DPLA occasionally returns structured (dict) creator entries.
    names = [str(c) for c in creators] if isinstance(creators, list) else []
    date = display_date(source.get("date"))
    provider = doc.get("provider")
    is_shown_at = doc.get("isShownAt")
    title_text = str(title or "")
    manifest = find_manifest(doc)
    raw = {
        "title": title_text,
        "creators": names,
        "date": date,
        "id": doc_id,
        "item_url": is_shown_at,
        "iiif_manifest": manifest,
        "isShownAt": is_shown_at,
        "isShownBy": doc.get("isShownBy"),
        "object": doc.get("object"),
        "hasView": doc.get("hasView"),
        "provider": provider.get("name") if isinstance(provider, Mapping) else None,
    }
    return SearchRecord(
        provider=KEY,
        source_id=doc_id,
        title=title_text,
        creators=as_creators(names),
        date=date,
        year=parse_year(date),
        item_url=is_shown_at if isinstance(is_shown_at, str) else None,
        iiif_manifest=manifest,
        raw=MappingProxyType(raw),
    )


class DplaProvider:
    """The DPLA adapter; ``source_id`` is the DPLA item id (32 hex digits)."""

    hosts: ClassVar[frozenset[str]] = frozenset({"dp.la"})

    def __init__(self, api_key: str | None, *, key_env: str = "DPLA_API_KEY") -> None:
        self._api_key = api_key or None
        self._key_env = key_env

    @property
    def key(self) -> str:
        return KEY

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(iiif=True, api_key=KEY)

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        """Return at most *limit* records for the title and creator.

        Raises:
            MissingApiKey: No key is set.
            ProviderBlocked: The request failed.
            InvalidResponse: The answer is not a JSON object.
        """
        if self._api_key is None:
            raise MissingApiKey(KEY, self._key_env)
        params: dict[str, Any] = {
            "q": query.title,
            "api_key": self._api_key,
            "page_size": limit,
        }
        if query.creator:
            params["sourceResource.creator"] = query.creator
        url = f"{API_BASE_URL}items"
        data = fetch_document(network, KEY, url, params=params)
        if not isinstance(data, Mapping):
            raise InvalidResponse(KEY, url, "not a JSON object")
        docs = data.get("docs")
        records = [parse_doc(doc) for doc in docs] if isinstance(docs, list) else []
        return [record for record in records if record is not None][:limit]

    def _details(
        self, network: Network, source_id: str, slot: SlotToken | None
    ) -> tuple[str, Mapping[str, Any]]:
        """Return the item endpoint's URL and answer.

        Raises:
            ItemNotFound: The id is empty or unknown.
            ProviderBlocked: The request failed.
            InvalidResponse: The answer is not a JSON object.
        """
        if not source_id:
            raise ItemNotFound(KEY, source_id)
        url = f"{API_BASE_URL}items/{source_id}"
        params = {"api_key": self._api_key} if self._api_key else None
        details = fetch_document(
            network, KEY, url, params=params, slot=slot, item=source_id
        )
        if not isinstance(details, Mapping):
            raise InvalidResponse(KEY, url, "not a JSON object")
        return url, details

    @staticmethod
    def _manifest_url(
        record_or_id: SearchRecord | str, doc: Mapping[str, Any]
    ) -> str | None:
        found = find_manifest(doc)
        if found is None and isinstance(record_or_id, SearchRecord):
            found = record_or_id.iiif_manifest
        return found if found and "manifest" in found else None

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        """Stage the item record and the manifest's files; else a preview.

        Raises:
            UnsupportedCapability: The item names no IIIF manifest; the
                message names the holding library's page (``isShownAt``).
            ItemNotFound: The item or its manifest is unknown.
            ProviderBlocked: The item or manifest request failed.
            InvalidResponse: The item or manifest is not a JSON object.
            OSError: A file could not be written.
        """
        source_id = source_id_of(record_or_id)
        url, details = self._details(network, source_id, request.slot)
        doc = item_doc(details)
        manifest_url = self._manifest_url(record_or_id, doc)
        if manifest_url is None:
            raise UnsupportedCapability(KEY, "download", detail=_no_manifest(doc))
        collector = Collector(network, KEY, staging_dir, request)
        collector.document("item", details, source_url=url)
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
        if has_content(result) or result.stopped is not None:
            return collector.result(source_id, result.parts)
        search_raw = record_or_id.raw if isinstance(record_or_id, SearchRecord) else {}
        collector.preview(fallback_urls(doc, search_raw))
        return collector.result(source_id, result.parts)

    def pages(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        first_n: int,
        max_px: int,
    ) -> list[PageRef]:
        """Return the manifest's first canvases; none without a manifest.

        Raises:
            ItemNotFound, ProviderBlocked, InvalidResponse: As ``download``.
        """
        source_id = source_id_of(record_or_id)
        _url, details = self._details(network, source_id, None)
        manifest_url = self._manifest_url(record_or_id, item_doc(details))
        if manifest_url is None:
            return []
        manifest = fetch_json(network, KEY, manifest_url, item=source_id)
        return manifest_page_refs(manifest, first_n=first_n, max_px=max_px)

    def item_id_from_url(self, url: str) -> str | None:
        """Return the item id of an item page (``dp.la/item/<id>``) or of an
        API item URL (``api.dp.la/v2/items/<id>``)."""
        if not on_hosts(url, self.hosts):
            return None
        return path_match(url, _ITEM_PATH)


def build(settings: Settings) -> DplaProvider:
    """Return the adapter with the key from the environment variable."""
    key_env = settings.api_key_env(KEY)
    return DplaProvider(os.environ.get(key_env), key_env=key_env)
