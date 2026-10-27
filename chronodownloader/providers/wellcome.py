"""Wellcome Collection: catalogue search and IIIF downloads.

Ported from ChronoDownloader v1.30.3 ``api/providers/wellcome.py`` on
19.10.2026. Catalogue: https://developers.wellcomecollection.org/api/catalogue;
IIIF: https://developers.wellcomecollection.org/docs/iiif.

Search asks ``/catalogue/v2/works`` with ``include=items,contributors,
production``. Wellcome used to expose digitised content as ``iiif-image``
locations pointing at an ``info.json``; live responses now carry
``iiif-presentation`` manifest URLs instead. Both location types are
accepted: a manifest is downloaded through the shared IIIF strategy, a list
of image services as a manifest built from them. Changed: page images are
capped at 1,024 px on the longer side (:attr:`Capabilities.image_max_px`;
larger sizes are refused without authentication), the page limit is the
run's ``max_pages`` (formerly ``max_images``), and the thumbnail bonus file
is gone.

``source_id`` is the catalogue work id.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar

from chronodownloader.iiif import image_url_candidates
from chronodownloader.net import Network
from chronodownloader.settings import Settings

from ._file_download import get_json
from ._query import parse_year
from ._urls import on_hosts, path_match
from .base import (
    Capabilities,
    Completeness,
    DownloadRequest,
    DownloadResult,
    PageRef,
    SearchQuery,
    SearchRecord,
    StagedFile,
    page_item,
    source_id_of,
)
from .iiif_download import download_manifest, fetch_json, manifest_page_refs
from .staging import stage_metadata

__all__ = ["CATALOGUE_WORKS_URL", "IMAGE_MAX_PX", "Wellcome", "build"]

logger = logging.getLogger(__name__)

KEY = "wellcome"
CATALOGUE_WORKS_URL = "https://api.wellcomecollection.org/catalogue/v2/works"
IMAGE_MAX_PX = 1024
"""The longest side Wellcome serves without authentication."""
SEARCH_INCLUDE = "items,contributors,production"
MAX_PAGE_SIZE = 100
"""The catalogue's ``pageSize`` cap; a larger value answers 400."""
WORK_NAME = "work"
"""The staged image-service list of a legacy work (``metadata/work.json``)."""
_WORK_PATH = re.compile(r"^/works/([a-z0-9]+)(?:[/?#]|$)", re.IGNORECASE)


def _locations(work: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    for item in work.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        for location in item.get("locations") or []:
            if isinstance(location, Mapping):
                found.append(location)
    return found


def _location_type(location: Mapping[str, Any]) -> Any:
    kind = location.get("locationType")
    return kind.get("id") if isinstance(kind, Mapping) else None


def extract_image_services(work: Mapping[str, Any]) -> list[str]:
    """Return the Image API service bases of a work's ``iiif-image`` locations.

    Each such location's URL ends in ``/info.json``; the base is the URL
    without it.
    """
    services: list[str] = []
    for location in _locations(work):
        if _location_type(location) != "iiif-image":
            continue
        url = location.get("url") or ""
        if isinstance(url, str) and url.endswith("/info.json"):
            services.append(url[: -len("/info.json")])
    return services


def extract_manifest_urls(work: Mapping[str, Any]) -> list[str]:
    """Return the work's ``iiif-presentation`` manifest URLs, without repeats."""
    manifests: list[str] = []
    for location in _locations(work):
        if _location_type(location) != "iiif-presentation":
            continue
        url = location.get("url")
        if url and str(url) not in manifests:
            manifests.append(str(url))
    return manifests


def first_contributor(work: Mapping[str, Any]) -> str | None:
    """Return the label of the work's first named contributor, or None."""
    for contributor in work.get("contributors") or []:
        if not isinstance(contributor, Mapping):
            continue
        agent = contributor.get("agent")
        label = agent.get("label") if isinstance(agent, Mapping) else None
        if label:
            return str(label).strip() or None
    return None


def production_date(work: Mapping[str, Any]) -> str | None:
    """Return the work's printed publication date label ("[1890]"), or None."""
    for event in work.get("production") or []:
        if not isinstance(event, Mapping):
            continue
        for date in event.get("dates") or []:
            if isinstance(date, Mapping) and date.get("label"):
                return str(date["label"]).strip() or None
    return None


def _record(work: Any) -> SearchRecord | None:
    """Return the record of a digitised work; None for anything else."""
    if not isinstance(work, Mapping):
        logger.warning("%s: skipping malformed record %r", KEY, work)
        return None
    services = extract_image_services(work)
    manifests = extract_manifest_urls(work)
    if not services and not manifests:
        return None
    work_id = work.get("id")
    if not isinstance(work_id, str) or not work_id:
        logger.warning("%s: skipping digitised work without id", KEY)
        return None
    creator = first_contributor(work)
    date = production_date(work)
    thumbnail = work.get("thumbnail")
    item_url = f"https://wellcomecollection.org/works/{work_id}"
    manifest = manifests[0] if manifests else None
    raw = {
        "title": work.get("title") or "",
        "creator": creator,
        "date": date,
        "id": work_id,
        "item_url": item_url,
        "image_services": services,
        "iiif_manifest": manifest,
        "thumbnail": thumbnail.get("url") if isinstance(thumbnail, Mapping) else None,
    }
    return SearchRecord(
        provider=KEY,
        source_id=work_id,
        title=str(raw["title"]),
        creators=(creator,) if creator else (),
        date=date,
        year=parse_year(date),
        item_url=item_url,
        iiif_manifest=manifest,
        raw=MappingProxyType(raw),
    )


def _services_manifest(services: Sequence[str]) -> dict[str, Any]:
    """Return a v2 manifest with one canvas per Image API service."""
    canvases = [
        {"images": [{"resource": {"service": {"@id": service}}}]}
        for service in services
    ]
    return {
        "@context": "http://iiif.io/api/presentation/2/context.json",
        "sequences": [{"canvases": canvases}],
    }


@dataclass(frozen=True)
class _Sources:
    """Where a work's pages come from: image services or a manifest."""

    services: tuple[str, ...] = ()
    manifest_url: str | None = None


@dataclass(frozen=True)
class Wellcome:
    """The Wellcome Collection adapter (IIIF, 1,024 px image cap).

    ``works_url`` exists for tests.
    """

    hosts: ClassVar[frozenset[str]] = frozenset({"wellcomecollection.org"})
    works_url: str = CATALOGUE_WORKS_URL

    @property
    def key(self) -> str:
        """The provider key."""
        return KEY

    @property
    def capabilities(self) -> Capabilities:
        """What the adapter offers."""
        return Capabilities(iiif=True, image_max_px=IMAGE_MAX_PX)

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        """Return the digitised works matching title and creator.

        A few more works than *limit* are asked for, since works without
        digitised content are dropped.

        Raises:
            ProviderBlocked, InvalidResponse: The request failed.
        """
        text = f"{query.title} {query.creator}" if query.creator else query.title
        params = {
            "query": text,
            "include": SEARCH_INCLUDE,
            "pageSize": min(MAX_PAGE_SIZE, max(25, limit * 5)),
        }
        logger.info("Searching Wellcome Collection for: %s", query.title)
        data = get_json(network, KEY, self.works_url, params=params)
        works = data.get("results")
        records: list[SearchRecord] = []
        for work in works if isinstance(works, list) else []:
            record = _record(work)
            if record is None:
                continue
            records.append(record)
            if len(records) >= limit:
                break
        return records

    def _sources(
        self,
        network: Network,
        record_or_id: SearchRecord | str,
        request: DownloadRequest | None,
    ) -> _Sources:
        """Return the work's sources, from the record or a catalogue refetch.

        Raises:
            ItemNotFound, ProviderBlocked, InvalidResponse: The refetch failed.
        """
        services: list[str] = []
        manifest_url: str | None = None
        if isinstance(record_or_id, SearchRecord):
            raw_services = record_or_id.raw.get("image_services")
            if isinstance(raw_services, list | tuple):
                services = [str(service) for service in raw_services if service]
            manifest_url = record_or_id.iiif_manifest or record_or_id.raw.get(
                "iiif_manifest"
            )
        work_id = source_id_of(record_or_id).strip()
        if work_id and not services and not manifest_url:
            url = f"{self.works_url}/{urllib.parse.quote(work_id, safe='')}"
            slot = request.slot if request is not None else None
            work = get_json(
                network, KEY, url, params={"include": "items"}, slot=slot, item=work_id
            )
            services = extract_image_services(work)
            manifests = extract_manifest_urls(work)
            manifest_url = manifests[0] if manifests else None
        return _Sources(tuple(services), str(manifest_url) if manifest_url else None)

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        """Stage the work's rendering and/or page images at most 1,024 px.

        A work without digitised content returns an empty, incomplete
        images part.

        Raises:
            ItemNotFound, ProviderBlocked, InvalidResponse: The work or its
                manifest could not be had.
            OSError: A file could not be written.
        """
        work_id = source_id_of(record_or_id).strip()
        sources = self._sources(network, record_or_id, request)
        if not sources.services and sources.manifest_url:
            manifest = fetch_json(
                network, KEY, sources.manifest_url, slot=request.slot, item=work_id
            )
            return download_manifest(
                network,
                KEY,
                manifest,
                source_id=work_id,
                staging_dir=staging_dir,
                request=request,
                manifest_url=sources.manifest_url,
                image_max_px=IMAGE_MAX_PX,
            )
        if not sources.services:
            logger.info("%s: work %s has no digitised content", KEY, work_id)
            return DownloadResult(
                provider=KEY,
                source_id=work_id,
                format=request.format,
                parts=(Completeness("images", (), ()),),
            )
        staging_dir.mkdir(parents=True, exist_ok=True)
        staged: list[StagedFile] = []
        budget = request.budget
        if request.include_metadata and (budget is None or budget.allows("metadata")):
            work = stage_metadata(
                staging_dir,
                WORK_NAME,
                {"id": work_id, "image_services": list(sources.services)},
            )
            if budget is not None:
                budget.charge(work.kind, work.size)
            staged.append(work)
        result = download_manifest(
            network,
            KEY,
            _services_manifest(sources.services),
            source_id=work_id,
            staging_dir=staging_dir,
            request=replace(request, include_metadata=False),
            image_max_px=IMAGE_MAX_PX,
        )
        return replace(result, files=(*staged, *result.files))

    def pages(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        first_n: int,
        max_px: int,
    ) -> list[PageRef]:
        """Return the work's first canvases at most *max_px* pixels.

        Raises:
            ValueError: *first_n* or *max_px* is below 1.
            ItemNotFound, ProviderBlocked, InvalidResponse: The work or its
                manifest could not be had.
        """
        if first_n < 1 or max_px < 1:
            raise ValueError("first_n and max_px must be at least 1")
        size = min(max_px, IMAGE_MAX_PX)
        sources = self._sources(network, record_or_id, None)
        if sources.services:
            return [
                PageRef(
                    page_item(index),
                    index,
                    tuple(image_url_candidates(service, max_px=size)),
                    service=service,
                )
                for index, service in enumerate(sources.services[:first_n], start=1)
            ]
        if not sources.manifest_url:
            return []
        manifest = fetch_json(
            network,
            KEY,
            sources.manifest_url,
            item=source_id_of(record_or_id),
        )
        return manifest_page_refs(manifest, first_n=first_n, max_px=size)

    def item_id_from_url(self, url: str) -> str | None:
        """Return the work id of a catalogue page (``/works/<id>``).

        The page URL may have ``/items`` after the id.
        """
        if not on_hosts(url, self.hosts):
            return None
        return path_match(url, _WORK_PATH)


def build(settings: Settings) -> Wellcome:
    """Return the adapter; it needs no settings beyond the network block."""
    del settings
    return Wellcome()
