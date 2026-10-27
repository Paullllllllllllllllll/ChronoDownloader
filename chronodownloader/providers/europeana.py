"""Europeana: Search API records, downloads through the IIIF manifest.

Ported from ChronoDownloader v1.30.3 ``api/providers/europeana.py`` on
19.10.2026. Kept: the query ``title:"..." AND who:"..."`` with embedded
double quotes stripped, three times the limit requested so that records with
a manifest are among them, the manifest discovery (``hasView``, ``object``,
else the Manifest API URL built from the record id), the API key (``wskey``)
added only to Europeana's own manifest host and only at fetch time, and the
Record API fallback to ``edmIsShownBy`` and ``edmPreview``. Changed: a
failed request raises instead of returning no results; a missing key raises
:class:`~.base.MissingApiKey`; the Record API fallback stages a preview
that never completes the work.

Many records are served by the contributing library: the page images of a
Gallica-backed record come from ``gallica.bnf.fr``. The shared download
strategy paces those requests under ``bnf_gallica`` and counts its refusals
(HTTP 403, cause ``blocked``) on Gallica's circuit breaker.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from chronodownloader.coord import SlotToken
from chronodownloader.net import Network
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
    source_id_of,
)
from .bnf_gallica import MANIFEST_URL as GALLICA_MANIFEST_URL
from .iiif_download import download_manifest, fetch_json, manifest_page_refs

__all__ = [
    "API_BASE_URL",
    "EUROPEANA_KEY_HOSTS",
    "EUROPEANA_MANIFEST_HOST",
    "KEY",
    "RECORD_API_BASE",
    "EuropeanaProvider",
    "append_wskey",
    "build",
    "build_manifest_url",
    "gallica_manifest_url",
    "parse_item",
    "record_media_urls",
    "search_query",
    "strip_tracking",
]

KEY = "europeana"
API_BASE_URL = "https://api.europeana.eu/record/v2/search.json"
RECORD_API_BASE = "https://api.europeana.eu/record/v2"
EUROPEANA_MANIFEST_HOST = "https://iiif.europeana.eu"
EUROPEANA_KEY_HOSTS = frozenset({"iiif.europeana.eu"})
"""The host names that receive the API key with a manifest request."""

_RECORD_PATH = re.compile(r"/(?:item|presentation)/(\w+/[^/?#]+)")
_GALLICA_RECORD = re.compile(r"/ark__12148_([A-Za-z0-9]+)$")


def build_manifest_url(euro_id: str, *, prefer_v3: bool = True) -> str | None:
    """Return the Manifest API URL of a record id such as ``/9200379/Bib_3``.

    The key is left out: this URL is stored with the candidate; it is added
    at fetch time by :func:`append_wskey`. An id without a collection and a
    record part gives None.
    """
    parts = [part for part in euro_id.strip().split("/") if part]
    if len(parts) < 2:
        return None
    collection_id, record_id = parts[-2], parts[-1]
    url = f"{EUROPEANA_MANIFEST_HOST}/presentation/{collection_id}/{record_id}/manifest"
    return f"{url}?format=3" if prefer_v3 else url


def gallica_manifest_url(euro_id: str) -> str | None:
    """Return Gallica's own manifest URL for a Gallica record id, else None.

    Gallica record ids read ``/794/ark__12148_bpt6k114423k``.
    """
    match = _GALLICA_RECORD.search(euro_id.strip())
    return GALLICA_MANIFEST_URL.format(ark_id=match.group(1)) if match else None


def _is_own_manifest_host(parts: SplitResult) -> bool:
    """Return True for an HTTPS URL on Europeana's own IIIF host.

    The URL must name the exact host, the default port and no user
    information.
    """
    try:
        port = parts.port
    except ValueError:
        return False
    return (
        parts.scheme.lower() == "https"
        and (parts.hostname or "") in EUROPEANA_KEY_HOSTS
        and port in (None, 443)
        and parts.username is None
        and parts.password is None
        and "@" not in parts.netloc
    )


def append_wskey(url: str, api_key: str | None) -> str:
    """Add the key to a manifest URL on Europeana's own IIIF host.

    Only that host understands ``wskey``; manifests on a contributing
    library's host, and any URL whose parsed host is not exactly
    Europeana's, are left untouched. The parameter is added to the parsed
    query, replacing none already there.
    """
    if not api_key:
        return url
    parts = urlsplit(url)
    if not _is_own_manifest_host(parts):
        return url
    query = parse_qsl(parts.query, keep_blank_values=True)
    if any(name == "wskey" for name, _value in query):
        return url
    return urlunsplit(parts._replace(query=urlencode([*query, ("wskey", api_key)])))


def search_query(title: str, creator: str | None) -> str:
    """Return the Search API query; embedded double quotes become spaces."""
    safe_title = title.replace('"', " ")
    query = f'title:"{safe_title}"'
    if creator:
        safe_creator = creator.replace('"', " ")
        query += f' AND who:"{safe_creator}"'
    return query


def _first_text(value: Any) -> Any:
    if isinstance(value, list):
        return value[0] if value else None
    return value


def _view_manifest(item: Mapping[str, Any]) -> str | None:
    cho = item.get("edmAggregatedCHO")
    views = cho.get("hasView") if isinstance(cho, Mapping) else None
    for view in views if isinstance(views, list) else [views]:
        if isinstance(view, str) and "manifest" in view:
            return view
        if isinstance(view, Mapping):
            view_id = view.get("@id")
            if isinstance(view_id, str) and "manifest" in view_id:
                return view_id
    return None


def _item_manifest(item: Mapping[str, Any], euro_id: str) -> str | None:
    manifest = _view_manifest(item)
    if manifest is None:
        obj = item.get("object")
        if isinstance(obj, str) and "manifest" in obj:
            manifest = obj
    return manifest or build_manifest_url(euro_id)


def strip_tracking(url: str) -> str:
    """Remove the ``utm_*`` parameters from an item URL.

    The Search API returns each ``guid`` with ``utm_campaign`` set to the
    caller's API key; stored as is, the key would reach the store and
    ``index.csv``.
    """
    parts = urlsplit(url)
    query = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if not name.startswith("utm_")
    ]
    return urlunsplit(parts._replace(query=urlencode(query)))


def parse_item(item: Any) -> SearchRecord | None:
    """Return the record of one Search API item, or None without an id."""
    if not isinstance(item, Mapping):
        return None
    euro_id = item.get("id")
    if not isinstance(euro_id, str) or not euro_id:
        return None
    # A present-but-empty "title": [] must give an empty title, not raise.
    title = _first_text(item.get("title") or [""]) or ""
    creator = _first_text(item.get("dcCreator")) or None
    data_provider = _first_text(item.get("dataProvider")) or None
    year = _first_text(item.get("year"))
    date = str(year) if year else None
    guid = item.get("guid") if isinstance(item.get("guid"), str) else None
    if guid:
        guid = strip_tracking(guid)
    manifest = _item_manifest(item, euro_id)
    raw = {
        "title": str(title),
        "creator": creator,
        "date": date,
        "id": euro_id,
        "item_url": guid,
        "europeana_url": guid,
        "provider": data_provider,
        "iiif_manifest": manifest,
    }
    return SearchRecord(
        provider=KEY,
        source_id=euro_id,
        title=str(title),
        creators=as_creators(creator),
        date=date,
        year=parse_year(date),
        item_url=guid,
        iiif_manifest=manifest,
        raw=MappingProxyType(raw),
    )


def record_media_urls(record: Any) -> list[str]:
    """Return a Record API v2 record's media links, full media first.

    The links sit under ``object``: ``edmIsShownBy`` and ``edmPreview`` on
    each aggregation, then the thumbnail on ``europeanaAggregation``.
    """
    obj = record.get("object") if isinstance(record, Mapping) else None
    if not isinstance(obj, Mapping):
        return []
    urls: list[str] = []
    aggregations = obj.get("aggregations")
    for aggregation in aggregations if isinstance(aggregations, list) else []:
        if isinstance(aggregation, Mapping):
            for name in ("edmIsShownBy", "edmPreview"):
                value = aggregation.get(name)
                if isinstance(value, str) and value:
                    urls.append(value)
    europeana = obj.get("europeanaAggregation")
    if isinstance(europeana, Mapping):
        preview = europeana.get("edmPreview")
        if isinstance(preview, str) and preview:
            urls.append(preview)
    return urls


class EuropeanaProvider:
    """The Europeana adapter.

    ``source_id`` is the record id, such as ``/794/ark__12148_bpt6k114423k``.
    """

    hosts: ClassVar[frozenset[str]] = frozenset({"europeana.eu"})

    def __init__(
        self, api_key: str | None, *, key_env: str = "EUROPEANA_API_KEY"
    ) -> None:
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

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        """Return at most *limit* records for the title and creator.

        Raises:
            MissingApiKey: No key is set.
            ProviderBlocked: The request failed.
            InvalidResponse: The answer is not a successful Search API object.
        """
        if self._api_key is None:
            raise MissingApiKey(KEY, self._key_env)
        params = {
            "wskey": self._api_key,
            "query": search_query(query.title, query.creator),
            "rows": str(limit * 3),
            "profile": "rich",
            "media": "true",
        }
        data = fetch_document(network, KEY, API_BASE_URL, params=params)
        if not isinstance(data, Mapping):
            raise InvalidResponse(KEY, API_BASE_URL, "not a JSON object")
        if not data.get("success"):
            detail = str(data.get("error") or "success is false")
            raise InvalidResponse(KEY, API_BASE_URL, detail)
        items = data.get("items")
        records: list[SearchRecord] = []
        for item in items if isinstance(items, list) else []:
            record = parse_item(item)
            if record is not None:
                records.append(record)
            if len(records) >= limit:
                break
        return records

    def _manifest_url(self, record_or_id: SearchRecord | str) -> str:
        """Return the manifest URL to download from.

        That is Gallica's manifest for a Gallica record, else the stored
        manifest URL, else the one built from the id.

        Europeana's manifest of a Gallica record (``/794/ark__12148_...``)
        lists one canvas, whose image lies on a Gallica path that answers
        404 (``view3if``); Gallica's own manifest lists every page.

        Raises:
            ItemNotFound: There is no manifest URL and no record id to build
                one from (a title is never built into a URL).
        """
        source_id = source_id_of(record_or_id)
        gallica = gallica_manifest_url(source_id or "")
        if gallica:
            return gallica
        if isinstance(record_or_id, SearchRecord) and record_or_id.iiif_manifest:
            return record_or_id.iiif_manifest
        built = build_manifest_url(source_id) if source_id else None
        if built is None:
            raise ItemNotFound(KEY, source_id)
        return built

    def manifest_url(self, url: str) -> str:
        """Return *url* as this adapter's downloads fetch it.

        On Europeana's own manifest host the key is appended
        (:func:`append_wskey`).
        """
        return append_wskey(url, self._api_key)

    def _fetch_manifest(
        self, network: Network, url: str, source_id: str, slot: SlotToken | None
    ) -> Mapping[str, Any]:
        fetch_url = self.manifest_url(url)
        return fetch_json(network, KEY, fetch_url, slot=slot, item=source_id)

    def _record_url(self, source_id: str) -> str:
        return f"{RECORD_API_BASE}/{source_id.strip('/')}.json"

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        """Stage the manifest's rendering or pages; else a Record API preview.

        Raises:
            ItemNotFound: No manifest URL can be had, or the manifest is gone.
            ProviderBlocked: The manifest request failed.
            InvalidResponse: The manifest is not a JSON object.
            OSError: A file could not be written.
        """
        source_id = source_id_of(record_or_id)
        url = self._manifest_url(record_or_id)
        manifest = self._fetch_manifest(network, url, source_id, request.slot)
        collector = Collector(network, KEY, staging_dir, request)
        result = download_manifest(
            network,
            KEY,
            manifest,
            source_id=source_id,
            staging_dir=staging_dir,
            request=request,
            manifest_url=url,
        )
        collector.absorb(result)
        if not has_content(result) and result.stopped is None and source_id:
            params = {"wskey": self._api_key} if self._api_key else None
            record = collector.optional_document(
                self._record_url(source_id), params=params
            )
            collector.preview(record_media_urls(record))
        return collector.result(source_id, result.parts)

    def pages(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        first_n: int,
        max_px: int,
    ) -> list[PageRef]:
        """Return the manifest's first canvases at the reduced size.

        Raises:
            ItemNotFound, ProviderBlocked, InvalidResponse: As ``download``.
        """
        source_id = source_id_of(record_or_id)
        url = self._manifest_url(record_or_id)
        manifest = self._fetch_manifest(network, url, source_id, None)
        return manifest_page_refs(manifest, first_n=first_n, max_px=max_px)

    def item_id_from_url(self, url: str) -> str | None:
        """Return the record id of an item page or of a Manifest API URL.

        The id reads ``/<dataset>/<record>``; item pages read
        ``www.europeana.eu/<lang>/item/<dataset>/<record>``, Manifest API URLs
        ``iiif.europeana.eu/presentation/...``.
        """
        if not on_hosts(url, self.hosts):
            return None
        match = path_match(url, _RECORD_PATH)
        return f"/{match}" if match else None

    def item_manifest_url(self, item_id: str) -> str | None:
        """Return the manifest URL without the key.

        That is Gallica's manifest for a Gallica record, else the record's
        Manifest API URL.
        """
        return gallica_manifest_url(item_id) or build_manifest_url(item_id)


def build(settings: Settings) -> EuropeanaProvider:
    """Return the adapter with the key from the environment variable."""
    key_env = settings.api_key_env(KEY)
    return EuropeanaProvider(os.environ.get(key_env), key_env=key_env)
