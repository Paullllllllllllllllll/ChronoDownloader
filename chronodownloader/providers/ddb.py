"""DDB: Deutsche Digitale Bibliothek records, downloads from the holding library.

Ported from ChronoDownloader v1.30.3 ``api/providers/ddb.py`` on 19.10.2026.
DDB aggregates German institutions; most records link to the holding
library, which serves its own IIIF manifest. Kept: the quoted query with
embedded double quotes stripped, ``<match>`` tags stripped from titles, the
author read from the ``view`` entry with a ``Verfasser`` or ``Autor`` role
marker, and the manifest taken from the record's ``iiifManifest`` or built
from its ``isShownAt`` link for Heidelberg, Göttingen and MDZ. Changed: a
failed request raises instead of returning no results; a missing key raises
:class:`~.base.MissingApiKey`. A download fetches only
the record it is given: a record without a manifest stages its
``isShownBy`` image as a preview and reports the work missing (in the
19.10.2026 baseline v1.30.3's pipeline then downloaded another candidate's
MDZ copy under the first record's id).
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
from chronodownloader.settings import Settings

from ._support import (
    Collector,
    as_creators,
    fetch_document,
    has_content,
)
from ._urls import on_hosts, path_match
from .base import (
    Capabilities,
    Completeness,
    DownloadRequest,
    DownloadResult,
    InvalidResponse,
    ItemNotFound,
    MissingApiKey,
    PageRef,
    SearchQuery,
    SearchRecord,
    source_id_of,
)
from .iiif_download import download_manifest, fetch_json, manifest_page_refs

__all__ = [
    "API_BASE_URL",
    "IIIF_MANIFEST_PATTERNS",
    "ITEM_URL_BASE",
    "KEY",
    "DdbProvider",
    "aggregation_links",
    "build",
    "creator_from_view",
    "manifest_url_from_link",
    "parse_doc",
    "search_query",
]

KEY = "ddb"
API_BASE_URL = "https://api.deutsche-digitale-bibliothek.de/"
ITEM_URL_BASE = "https://www.deutsche-digitale-bibliothek.de/item/"

IIIF_MANIFEST_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Heidelberg University Library. Stop at "?" and "#" as well as "/": a
    # tracking query string otherwise rode into the manifest path.
    (
        re.compile(r"digi\.ub\.uni-heidelberg\.de/diglit/([^/?#]+)"),
        "https://digi.ub.uni-heidelberg.de/diglit/iiif/{}/manifest.json",
    ),
    # Göttingen State and University Library.
    (
        re.compile(r"resolver\.sub\.uni-goettingen\.de/purl\?([^/&]+)"),
        "https://manifests.sub.uni-goettingen.de/iiif/presentation/{}/manifest",
    ),
    # Bavarian State Library (BSB/MDZ). The BSB number sits at a varying
    # depth: /view/bsb..., /en/view/bsb..., /de/view/bsb... and the urn:nbn
    # resolver's /bsb:/... form all occur in DDB's isShownAt.
    (
        re.compile(r"(?:mdz-nbn-resolving\.de|digitale-sammlungen\.de)\S*?(bsb\d+)"),
        "https://api.digitale-sammlungen.de/iiif/presentation/v2/{}/manifest",
    ),
)
"""Holding-library link shapes and the manifest URL each one gives."""

# DDB's "view" array is an unlabeled, heterogeneous list of display snippets
# whose length varies per record, so a fixed index means nothing: index 6
# returned a document type, a publisher and an extent statement as the
# author. Entries that really are people carry a German role marker.
_VIEW_CREATOR = re.compile(r"^(.*?)\s*\((?:Verfasser|Autor)\S*\)$")
_ITEM_PATH = re.compile(r"/items?/([A-Z0-9]{32})(?:[/?#]|$)")


def manifest_url_from_link(is_shown_at: str | None) -> str | None:
    """Return the manifest URL a holding library's link implies, or None."""
    if not is_shown_at:
        return None
    for pattern, template in IIIF_MANIFEST_PATTERNS:
        match = pattern.search(is_shown_at)
        if match:
            return template.format(match.group(1))
    return None


def creator_from_view(view: Any) -> str | None:
    """Return the author of a ``view`` array without its role marker."""
    if not isinstance(view, list):
        return None
    for entry in view:
        if not isinstance(entry, str):
            continue
        match = _VIEW_CREATOR.match(entry.strip())
        if match and match.group(1).strip():
            return match.group(1).strip()
    return None


def search_query(title: str, creator: str | None) -> str:
    """Return the search query; embedded double quotes become spaces."""
    safe_title = title.replace('"', " ")
    query = f'"{safe_title}"'
    if creator:
        safe_creator = creator.replace('"', " ")
        query += f' AND creator:"{safe_creator}"'
    return query


def parse_doc(doc: Any) -> SearchRecord | None:
    """Return the record of one search doc, or None without an id."""
    if not isinstance(doc, Mapping):
        return None
    ddb_id = doc.get("id")
    if not isinstance(ddb_id, str) or not ddb_id:
        return None
    title = doc.get("label") or doc.get("title") or ""
    if isinstance(title, list):
        title = title[0] if title else ""
    title = str(title).replace("<match>", "").replace("</match>", "")
    creator = creator_from_view(doc.get("view"))
    item_url = f"{ITEM_URL_BASE}{ddb_id}"
    raw = {
        "title": title,
        "creator": creator,
        "id": ddb_id,
        "item_url": item_url,
        "thumbnail": doc.get("thumbnail"),
        "iiif_manifest": None,
    }
    return SearchRecord(
        provider=KEY,
        source_id=ddb_id,
        title=title,
        creators=as_creators(creator),
        item_url=item_url,
        raw=MappingProxyType(raw),
    )


def _resource(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("@resource")
    return value if isinstance(value, str) and value else None


def aggregation_links(meta: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """Return the ``isShownAt`` and ``isShownBy`` links of an item's EDM."""
    node: Any = meta
    for key in ("edm", "RDF", "Aggregation"):
        node = node.get(key) if isinstance(node, Mapping) else None
    if not isinstance(node, Mapping):
        return None, None
    return _resource(node.get("isShownAt")), _resource(node.get("isShownBy"))


class DdbProvider:
    """The DDB adapter; ``source_id`` is the DDB item id (32 characters)."""

    hosts: ClassVar[frozenset[str]] = frozenset({"deutsche-digitale-bibliothek.de"})

    def __init__(self, api_key: str | None, *, key_env: str = "DDB_API_KEY") -> None:
        self._api_key = api_key or None
        self._key_env = key_env

    @property
    def key(self) -> str:
        """The provider key."""
        return KEY

    @property
    def capabilities(self) -> Capabilities:
        """What the adapter offers."""
        return Capabilities(iiif=True, api_key=KEY)

    def _params(self) -> dict[str, str] | None:
        return {"oauth_consumer_key": self._api_key} if self._api_key else None

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
        params = {
            "oauth_consumer_key": self._api_key,
            "query": search_query(query.title, query.creator),
            "rows": limit,
        }
        url = f"{API_BASE_URL}search"
        data = fetch_document(network, KEY, url, params=params)
        if not isinstance(data, Mapping):
            raise InvalidResponse(KEY, url, "not a JSON object")
        groups = data.get("results")
        records: list[SearchRecord] = []
        for group in groups if isinstance(groups, list) else []:
            # A group without docs, or with "docs": null, is empty.
            docs = group.get("docs") if isinstance(group, Mapping) else None
            for doc in docs if isinstance(docs, list) else []:
                record = parse_doc(doc)
                if record is not None:
                    records.append(record)
        return records[:limit]

    def _item(
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
        meta = fetch_document(
            network, KEY, url, params=self._params(), slot=slot, item=source_id
        )
        if not isinstance(meta, Mapping):
            raise InvalidResponse(KEY, url, "not a JSON object")
        return url, meta

    @staticmethod
    def _manifest_url(
        record_or_id: SearchRecord | str, meta: Mapping[str, Any]
    ) -> str | None:
        """Return the record's manifest, else the one its holding library implies.

        That manifest is derived from the holding library's ``isShownAt`` link
        and belongs to this same record.
        """
        manifest = meta.get("iiifManifest")
        if isinstance(manifest, str) and manifest:
            return manifest
        if isinstance(record_or_id, SearchRecord) and record_or_id.iiif_manifest:
            return record_or_id.iiif_manifest
        return manifest_url_from_link(aggregation_links(meta)[0])

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        """Stage the record and the files of the holding library's manifest.

        When no manifest yields files, the record's ``isShownBy`` image is
        staged as a preview.

        Raises:
            ItemNotFound: The record or its manifest is unknown.
            ProviderBlocked: The record or manifest request failed.
            InvalidResponse: The record or manifest is not a JSON object.
            OSError: A file could not be written.
        """
        source_id = source_id_of(record_or_id)
        url, meta = self._item(network, source_id, request.slot)
        collector = Collector(network, KEY, staging_dir, request)
        collector.document("item", meta, source_url=url)
        manifest_url = self._manifest_url(record_or_id, meta)
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
        collector.preview([aggregation_links(meta)[1]])
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

        Raises:
            ItemNotFound, ProviderBlocked, InvalidResponse: As ``download``.
        """
        source_id = source_id_of(record_or_id)
        _url, meta = self._item(network, source_id, None)
        manifest_url = self._manifest_url(record_or_id, meta)
        if manifest_url is None:
            return []
        manifest = fetch_json(network, KEY, manifest_url, item=source_id)
        return manifest_page_refs(manifest, first_n=first_n, max_px=max_px)

    def item_id_from_url(self, url: str) -> str | None:
        """Return the 32-character item id of an item page or of an API item URL.

        These read ``/item/<id>`` and ``/items/<id>``.
        """
        if not on_hosts(url, self.hosts):
            return None
        return path_match(url, _ITEM_PATH)


def build(settings: Settings) -> DdbProvider:
    """Return the adapter with the key from the environment variable."""
    key_env = settings.api_key_env(KEY)
    return DdbProvider(os.environ.get(key_env), key_env=key_env)
