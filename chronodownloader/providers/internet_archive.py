"""Internet Archive: advanced search, item metadata, files and page images.

Ported from ChronoDownloader v1.30.3 ``api/providers/internet_archive.py`` on
19.10.2026. The search query, the record fields and the file rules for the
whole-document file carry over. Changed: page images come from the Archive's
own page service, ``https://archive.org/download/<id>/page/n<k>.jpg`` (the
BookReader image endpoint behind a redirect; verified on 19.10.2026 for
``bub_gb_fu0qAAAAYAAJ``, page ``n0`` 319 kB in 4 s), because the IIIF image
server stalls on large items; a page the service cannot deliver falls back
to the canvas of the item's IIIF manifest. Thumbnails and covers, which
never counted as content, are no longer fetched, and DjVu files are not
taken as renderings (staging accepts PDF and EPUB only). A search error the
Archive returns in a 200 body raises :class:`InvalidResponse` instead of
yielding no results.

``source_id`` is the item identifier.
"""

from __future__ import annotations

import logging
import posixpath
import re
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar

from chronodownloader.coord import SlotLost
from chronodownloader.iiif import Canvas, Rendering, extract_canvases
from chronodownloader.net import BreakerOpen, Network, QuotaSpent, RequestFailed
from chronodownloader.settings import Settings

from ._file_download import DownloadRun, get_json, wants_images
from ._query import parse_year
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
    StagedFile,
    page_item,
    provider_error,
    source_id_of,
)
from .iiif_download import manifest_page_refs, stage_canvas
from .staging import stage_first

__all__ = [
    "DOWNLOAD_URL",
    "IIIF_MANIFEST_URLS",
    "METADATA_URL",
    "SEARCH_URL",
    "InternetArchive",
    "build",
]

logger = logging.getLogger(__name__)

KEY = "internet_archive"
SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/{identifier}"
DOWNLOAD_URL = "https://archive.org/download/{identifier}"
IIIF_MANIFEST_URLS = (
    "https://iiif.archive.org/iiif/{identifier}/manifest.json",
    "https://iiif.archivelab.org/iiif/{identifier}/manifest.json",
)
SEARCH_FIELDS = "identifier,title,creator,mediatype,year"
RENDERING_EXTENSIONS = (".pdf", ".epub")
"""Whole-document file types in order of preference (one file per item)."""
METADATA_NAME = "ia_metadata"
"""The staged metadata response (``metadata/ia_metadata.json``)."""

_ITEM_PATH = re.compile(r"^/(?:details|download|embed|stream|metadata)/([^/?#]+)")
_IIIF_PATH = re.compile(r"^/iiif/(?:[23]/)?([^/?#]+)/manifest")


def _creators(value: Any) -> list[str]:
    """Return the creator field as a list: one string or a list of them."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(entry) for entry in value]
    return []


def _record(doc: Any) -> SearchRecord | None:
    """Return the record of one search hit, or None for a malformed hit."""
    if not isinstance(doc, Mapping):
        logger.warning("%s: skipping malformed record %r", KEY, doc)
        return None
    identifier = doc.get("identifier")
    if not isinstance(identifier, str) or not identifier:
        logger.warning("%s: skipping record without identifier", KEY)
        return None
    creators = _creators(doc.get("creator"))
    year = doc.get("year")
    date = str(year) if year not in (None, "") else None
    item_url = f"https://archive.org/details/{identifier}"
    raw = {
        "title": doc.get("title") or "",
        "creators": creators,
        "identifier": identifier,
        "item_url": item_url,
        "year": year,
    }
    return SearchRecord(
        provider=KEY,
        source_id=identifier,
        title=str(raw["title"]),
        creators=tuple(creators),
        date=date,
        year=parse_year(date),
        item_url=item_url,
        raw=MappingProxyType(raw),
    )


def _file_entries(metadata: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    files = metadata.get("files")
    if not isinstance(files, list):
        return []
    return [entry for entry in files if isinstance(entry, Mapping)]


def _file_name(entry: Mapping[str, Any]) -> str:
    return str(entry.get("name") or entry.get("file") or "").strip()


def rendering_files(metadata: Mapping[str, Any]) -> list[str]:
    """Return the names of the item's whole-document files, preferred first.

    The v1.30.3 file rules: PDF before EPUB, in file-list order within a
    type; a derivative whose name contains ``_text`` is skipped; a file
    matches by its own extension, or by its declared format when the name
    has none (a substring test against the format alone also matched the
    DjVu text and XML derivatives).
    """
    names: list[str] = []
    for ext in RENDERING_EXTENSIONS:
        for entry in _file_entries(metadata):
            name = _file_name(entry)
            if not name:
                continue
            fmt = str(entry.get("format") or "").lower()
            source = str(entry.get("source") or "").lower()
            if source == "derivative" and "_text" in name.lower():
                continue
            name_ext = posixpath.splitext(name)[1].lower()
            if name_ext != ext and not (not name_ext and ext.lstrip(".") in fmt):
                continue
            if name not in names:
                names.append(name)
    return names


def page_count(metadata: Mapping[str, Any]) -> int:
    """Return the number of page images the page service offers, 0 if unknown.

    The ``page_numbers`` list of the metadata response counts the pages of
    the access format; without it the item's ``imagecount`` applies.
    """
    numbers = metadata.get("page_numbers")
    if isinstance(numbers, Mapping):
        pages = numbers.get("pages")
        if isinstance(pages, list) and pages:
            return len(pages)
    fields = metadata.get("metadata")
    if isinstance(fields, Mapping):
        value = fields.get("imagecount")
        if isinstance(value, int) and not isinstance(value, bool):
            return max(value, 0)
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return 0


@dataclass
class _Manifest:
    """The item's IIIF manifest, fetched on first use (one download call)."""

    fetch: Callable[[], Mapping[str, Any] | None]
    fetched: bool = False
    canvases: list[Canvas] = field(default_factory=list)

    def load(self) -> list[Canvas]:
        """Return the manifest's canvases, fetching the manifest once.

        Raises:
            BreakerOpen, QuotaSpent, SlotLost: The download must stop.
        """
        if not self.fetched:
            self.fetched = True
            manifest = self.fetch()
            self.canvases = extract_canvases(manifest) if manifest else []
        return self.canvases

    def canvas(self, index: int) -> Canvas | None:
        """Return the canvas at 1-based *index*, or None.

        Raises:
            BreakerOpen, QuotaSpent, SlotLost: The download must stop.
        """
        canvases = self.load()
        return canvases[index - 1] if 0 < index <= len(canvases) else None


@dataclass(frozen=True)
class InternetArchive:
    """The Internet Archive adapter.

    ``--format pdf`` takes the item's PDF (else EPUB) file when it has one;
    page images come from the page service, each falling back to its IIIF
    canvas. The endpoint fields exist for tests.
    """

    hosts: ClassVar[frozenset[str]] = frozenset({"archive.org", "archivelab.org"})
    search_url: str = SEARCH_URL
    metadata_url: str = METADATA_URL
    download_url: str = DOWNLOAD_URL
    manifest_urls: tuple[str, ...] = IIIF_MANIFEST_URLS

    @property
    def key(self) -> str:
        return KEY

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities()

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        """Search texts by title (and creator) through the advanced search.

        Raises:
            ProviderBlocked: The request failed.
            InvalidResponse: The answer is not JSON or reports an error.
        """
        safe_title = query.title.replace('"', " ")
        clauses = [f'title:("{safe_title}")']
        if query.creator:
            safe_creator = query.creator.replace('"', " ")
            clauses.append(f'creator:("{safe_creator}")')
        clauses.append("mediatype:(texts)")
        params = {
            "q": " AND ".join(clauses),
            "fl[]": SEARCH_FIELDS,
            "rows": str(limit),
            "page": "1",
            "output": "json",
        }
        logger.info("Searching Internet Archive for: %s", query.title)
        data = get_json(network, KEY, self.search_url, params=params)
        error = data.get("error")
        if error:
            raise InvalidResponse(KEY, self.search_url, str(error))
        response = data.get("response")
        docs = response.get("docs") if isinstance(response, Mapping) else None
        if not isinstance(docs, list):
            return []
        records = [record for doc in docs if (record := _record(doc)) is not None]
        return records[:limit]

    def _item_url(self, template: str, identifier: str) -> str:
        return template.format(identifier=urllib.parse.quote(identifier, safe=""))

    def _metadata(
        self, network: Network, identifier: str, request: DownloadRequest | None
    ) -> tuple[Mapping[str, Any], str]:
        """Return the item's metadata response and its URL.

        Raises:
            ItemNotFound: The identifier is empty or unknown (the Archive
                answers an unknown identifier with an empty object).
            ProviderBlocked, InvalidResponse: As :func:`get_json`.
        """
        if not identifier:
            raise ItemNotFound(KEY, identifier)
        url = self._item_url(self.metadata_url, identifier)
        slot = request.slot if request is not None else None
        metadata = get_json(network, KEY, url, slot=slot, item=identifier)
        if not metadata:
            raise ItemNotFound(KEY, identifier, url)
        return metadata, url

    def _file_url(self, identifier: str, name: str) -> str:
        base = self._item_url(self.download_url, identifier)
        return f"{base}/{urllib.parse.quote(name)}"

    def _page_url(self, identifier: str, index: int, height: int | None = None) -> str:
        """Return the page service URL of the page at 1-based *index*."""
        base = self._item_url(self.download_url, identifier)
        size = f"_h{height}" if height else ""
        return f"{base}/page/n{index - 1}{size}.jpg"

    def _manifest_candidates(
        self, identifier: str, metadata: Mapping[str, Any]
    ) -> list[str]:
        misc = metadata.get("misc")
        if isinstance(misc, Mapping):
            declared = misc.get("ia_iiif_url")
            if isinstance(declared, str) and declared:
                return [declared]
        return [self._item_url(url, identifier) for url in self.manifest_urls]

    def _fetch_manifest(
        self, network: Network, urls: Sequence[str], request: DownloadRequest | None
    ) -> Mapping[str, Any] | None:
        """Return the first manifest of *urls* that loads, or None.

        Raises:
            BreakerOpen, QuotaSpent, SlotLost: The download must stop.
        """
        slot = request.slot if request is not None else None
        for url in urls:
            try:
                response = network.request(KEY, "GET", url, slot=slot)
            except RequestFailed as exc:
                logger.info("%s: no IIIF manifest at %s: %s", KEY, url, exc)
                continue
            try:
                document = response.json()
            except ValueError:
                logger.info("%s: IIIF manifest at %s is not JSON", KEY, url)
                continue
            if isinstance(document, Mapping) and document:
                return document
        return None

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        """Stage the item's whole-document file and/or page images.

        Raises:
            ItemNotFound: The identifier is empty or unknown.
            ProviderBlocked, InvalidResponse: The metadata could not be had.
            OSError: A file could not be written.
        """
        identifier = source_id_of(record_or_id).strip()
        metadata, metadata_url = self._metadata(network, identifier, request)
        staging_dir.mkdir(parents=True, exist_ok=True)
        run = DownloadRun(network, KEY, staging_dir, request)
        run.stage_metadata(METADATA_NAME, metadata, source_url=metadata_url)
        offered = (
            [
                Rendering(self._file_url(identifier, name), "", name)
                for name in rendering_files(metadata)
            ]
            if request.format != "images"
            else []
        )
        parts: list[Completeness] = []
        rendering_failed = False
        if offered:
            pdf = run.stage_rendering(offered)
            parts.append(pdf)
            rendering_failed = not pdf.complete and run.stopped is None
        count = page_count(metadata)
        if wants_images(
            request.format,
            offered=bool(offered),
            rendering_failed=rendering_failed,
            has_pages=count > 0,
        ):
            parts.append(self._stage_pages(run, identifier, metadata, count))
        return run.result(identifier, parts)

    def _stage_pages(
        self,
        run: DownloadRun,
        identifier: str,
        metadata: Mapping[str, Any],
        count: int,
    ) -> Completeness:
        """Stage the page images within the page limit.

        Each page comes from the page service, else from its IIIF canvas;
        with no page count in the metadata the manifest's canvases are the
        pages. A page that cannot be had stays missing.
        """
        candidates = self._manifest_candidates(identifier, metadata)
        manifest = _Manifest(
            lambda: self._fetch_manifest(run.network, candidates, run.request)
        )
        limit = run.request.page_limit
        available = count
        direct = count > 0
        if not direct and run.halt is None:
            try:
                available = len(manifest.load())
            except (BreakerOpen, QuotaSpent, SlotLost) as exc:
                run.stop(exc)
        selected = range(1, (min(available, limit) if limit else available) + 1)
        expected = tuple(page_item(index) for index in selected)
        present = set(expected) & run.request.present
        for index in selected:
            item = page_item(index)
            if item in present:
                continue
            if run.halt is not None or not run.admits("image"):
                break
            try:
                staged = self._stage_page(run, identifier, index, manifest, direct)
            except (BreakerOpen, QuotaSpent, SlotLost) as exc:
                run.stop(exc)
                break
            if staged is None:
                logger.warning("%s: page %s could not be downloaded", KEY, item)
                continue
            run.keep(staged)
            present.add(item)
        return Completeness(
            "images",
            expected,
            tuple(item for item in expected if item in present),
            page_limit=limit,
            available=available,
        )

    def _stage_page(
        self,
        run: DownloadRun,
        identifier: str,
        index: int,
        manifest: _Manifest,
        direct: bool,
    ) -> StagedFile | None:
        """Stage one page: the page service first, then the IIIF canvas.

        Raises:
            BreakerOpen, QuotaSpent, SlotLost, OSError: The download must stop.
        """
        item = page_item(index)
        if direct:
            staged = stage_first(
                run.network,
                KEY,
                [self._page_url(identifier, index)],
                run.staging_dir,
                item,
                slot=run.slot,
                kind="image",
            )
            if isinstance(staged, StagedFile):
                return staged
            logger.info("%s: %s falls back to the IIIF canvas", KEY, item)
        canvas = manifest.canvas(index)
        if canvas is None or canvas.source is None:
            return None
        return stage_canvas(
            run.network,
            KEY,
            canvas.source,
            run.staging_dir,
            item,
            slot=run.slot,
            max_px=None,
        )

    def pages(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        first_n: int,
        max_px: int,
    ) -> list[PageRef]:
        """Return the first pages from the page service, else the manifest.

        The page service returns the smallest power-of-two reduction that is
        at least as tall as asked, so asking for half of *max_px* keeps the
        height within *max_px* (the width too, for upright pages).

        Raises:
            ValueError: *first_n* or *max_px* is below 1.
            ItemNotFound, ProviderBlocked, InvalidResponse: The metadata or
                the manifest could not be had.
        """
        if first_n < 1 or max_px < 1:
            raise ValueError("first_n and max_px must be at least 1")
        identifier = source_id_of(record_or_id).strip()
        metadata, _url = self._metadata(network, identifier, None)
        count = page_count(metadata)
        if count:
            height = (max_px + 1) // 2
            return [
                PageRef(
                    page_item(index),
                    index,
                    (self._page_url(identifier, index, height),),
                )
                for index in range(1, min(first_n, count) + 1)
            ]
        try:
            manifest = self._fetch_manifest(
                network, self._manifest_candidates(identifier, metadata), None
            )
        except (BreakerOpen, QuotaSpent, SlotLost) as exc:
            raise provider_error(KEY, exc, identifier) from exc
        if manifest is None:
            return []
        return manifest_page_refs(manifest, first_n=first_n, max_px=max_px)

    def item_id_from_url(self, url: str) -> str | None:
        """Return the identifier of an item URL (``/details/<id>``, with a
        ``/page/...`` view after it, ``/download/``, ``/embed/``,
        ``/stream/``, ``/metadata/``, or the IIIF manifest)."""
        if not on_hosts(url, self.hosts):
            return None
        return path_match(url, _ITEM_PATH) or path_match(url, _IIIF_PATH)

    def item_manifest_url(self, item_id: str) -> str:
        """Return the item's manifest URL on ``iiif.archive.org``."""
        return IIIF_MANIFEST_URLS[0].format(identifier=item_id)


def build(settings: Settings) -> InternetArchive:
    """Return the adapter; it needs no settings beyond the network block."""
    del settings
    return InternetArchive()
