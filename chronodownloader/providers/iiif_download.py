"""Download a IIIF item: manifest, rendering and page images, with completeness.

The provider-side half of the IIIF support, ported from ChronoDownloader
v1.30.3 ``api/iiif/_strategies.py``, ``_renderings.py``, ``_direct.py`` and
the download primitive of ``_parsing.py`` on 19.10.2026. Changed: every
request goes through the run's network client with the held download slot
(under which key: see "Foreign hosts"); files are staged by item (``p00001.jpg``,
``rendering.pdf``, ``metadata/manifest.json``); the result counts expected,
present and missing items per part instead of reporting success when one
page arrived (v1.30.3 recorded 3 of 500 pages as complete); a failed page
never stops the others, while an open breaker, a spent quota, a lost slot or
the size budget does.

A page with an Image API service is tried at up to
:data:`~chronodownloader.iiif.SPECULATIVE_CANDIDATES` speculative sizes,
then at the sizes its ``info.json`` advertises; every size respects the
provider's image size cap. Within one item, a :class:`SizeMemory` the
caller creates for that item puts the speculative form that staged the
last page first for the next page of the same service family; sizes from
``info.json`` are never carried over. A missing page or rendering whose
requests failed carries the last failure's cause in its part's
``causes``. The module keeps no state between calls.

Foreign hosts. An aggregator (Europeana, DPLA, DDB) often downloads from the
holding library. Every request here, and in the staging and support
helpers, is paced and breaker-checked under the key of the registered
provider that serves the URL's host (:func:`~.registry.serving_key`), else
under the calling provider's key, and takes that provider's network block
(delays, headers, impersonation, breaker settings). Each request's pacing
reservation checks the slot the caller holds for the work, whatever key it
is paced under; a transfer from another registered provider's host also
holds one of that provider's slots while it runs
(:func:`~.staging.serving`), so the holding library's pacing interval,
breaker and slot limit bind every agent that reaches it.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from chronodownloader.coord import SlotLost, SlotToken
from chronodownloader.iiif import (
    SPECULATIVE_CANDIDATES,
    Canvas,
    PageSource,
    extract_canvases,
    image_url_candidates,
    info_url,
    select_renderings,
)
from chronodownloader.net import BreakerOpen, Network, QuotaSpent, RequestFailed
from chronodownloader.net.credentials import redact_url

from ._file_download import DownloadRun
from .base import (
    Completeness,
    DownloadRequest,
    DownloadResult,
    InvalidResponse,
    PageRef,
    StagedFile,
    page_item,
    provider_error,
)
from .staging import serving, stage_first

__all__ = [
    "MANIFEST_NAME",
    "NO_SOURCE",
    "SizeMemory",
    "download_manifest",
    "fetch_info",
    "fetch_json",
    "manifest_page_refs",
    "stage_canvas",
    "stage_service_image",
]

logger = logging.getLogger(__name__)

MANIFEST_NAME = "manifest"
"""The staged manifest's metadata name (``metadata/manifest.json``)."""

NO_SOURCE = "no_source"
"""The cause of a canvas that names no usable image."""


def fetch_json(
    network: Network,
    provider: str,
    url: str,
    *,
    slot: SlotToken | None = None,
    item: str = "",
) -> Mapping[str, Any]:
    """Fetch a JSON object, such as a manifest.

    Errors name *url* with its credentials masked.

    Raises:
        ItemNotFound: The provider answered 404 or 410.
        ProviderBlocked: The request failed otherwise, the breaker is open,
            the quota is spent or the slot was lost.
        InvalidResponse: The body is not a JSON object.
    """
    try:
        with serving(network, url, provider, slot) as key:
            response = network.request(key, "GET", url, slot=slot)
    except (RequestFailed, BreakerOpen, QuotaSpent, SlotLost) as exc:
        raise provider_error(provider, exc, item or redact_url(url)) from exc
    try:
        document = response.json()
    except ValueError as exc:
        raise InvalidResponse(provider, redact_url(url), "not JSON") from exc
    if not isinstance(document, Mapping):
        raise InvalidResponse(provider, redact_url(url), "not a JSON object")
    return document


def fetch_info(
    network: Network, provider: str, service_base: str, *, slot: SlotToken | None
) -> Mapping[str, Any] | None:
    """Return a service's ``info.json``, or None when it cannot be had.

    Raises:
        BreakerOpen, QuotaSpent, SlotLost: The download must stop.
    """
    url = info_url(service_base)
    try:
        with serving(network, url, provider, slot) as key:
            response = network.request(key, "GET", url, slot=slot)
    except RequestFailed as exc:
        logger.info("%s: no info.json at %s: %s", provider, url, exc)
        return None
    try:
        document = response.json()
    except ValueError:
        return None
    return document if isinstance(document, Mapping) and document else None


def _family(base: str) -> str:
    """Return the URL a service base shares with its siblings: all but its id."""
    return base.rpartition("/")[0]


@dataclass
class SizeMemory:
    """The speculative size form that staged the last page of one item.

    ``form`` is the part of the URL after the service base (for example
    ``/full/max/0/default.jpg``), learned under services of ``family``. A
    caller creates one per item and passes it to every page of that item;
    sharing it across items, threads or providers is not supported.
    """

    family: str = ""
    form: str = ""

    def order(self, base: str, speculative: list[str]) -> list[str]:
        """Return *speculative* with the remembered form first, if it is one."""
        if not self.form or self.family != _family(base):
            return speculative
        preferred = base + self.form
        if preferred not in speculative:
            return speculative
        return [preferred, *(url for url in speculative if url != preferred)]

    def learn(self, base: str, url: str) -> None:
        """Remember the form of *url*, a speculative candidate of *base*."""
        self.family, self.form = _family(base), url[len(base) :]


def stage_service_image(  # noqa: PLR0913 - two optional records (failures, memory)
    network: Network,
    provider: str,
    service_base: str,
    staging_dir: Path,
    item: str,
    *,
    slot: SlotToken,
    max_px: int | None,
    failures: list[RequestFailed] | None = None,
    memory: SizeMemory | None = None,
) -> StagedFile | None:
    """Stage one page from an Image API service; None when no size works.

    The ``info.json`` sizes are tried only when every speculative size
    failed for a size cause (a server refusal ends the attempts). Every
    failed image request is appended to *failures*. With *memory*, the
    item's remembered speculative form goes first, and a speculative
    success becomes the remembered form; an ``info.json`` size never does.

    Raises:
        BreakerOpen, QuotaSpent, SlotLost, OSError: The download must stop.
    """
    tried: set[str] = set()
    base = service_base.rstrip("/")
    speculative = image_url_candidates(base, max_px=max_px)[:SPECULATIVE_CANDIDATES]
    if memory is not None:
        speculative = memory.order(base, speculative)
    for url in speculative:
        staged = stage_first(
            network,
            provider,
            [url],
            staging_dir,
            item,
            slot=slot,
            kind="image",
            tried=tried,
            failures=failures,
        )
        if isinstance(staged, StagedFile):
            if memory is not None:
                memory.learn(base, url)
            return staged
        if staged is not None:
            return None
    info = fetch_info(network, provider, service_base, slot=slot)
    if info is None:
        return None
    staged = stage_first(
        network,
        provider,
        image_url_candidates(service_base, info, max_px=max_px),
        staging_dir,
        item,
        slot=slot,
        kind="image",
        tried=tried,
        failures=failures,
    )
    return staged if isinstance(staged, StagedFile) else None


def stage_canvas(  # noqa: PLR0913 - two optional records (failures, memory)
    network: Network,
    provider: str,
    source: PageSource,
    staging_dir: Path,
    item: str,
    *,
    slot: SlotToken,
    max_px: int | None,
    failures: list[RequestFailed] | None = None,
    memory: SizeMemory | None = None,
) -> StagedFile | None:
    """Stage one canvas from its service or its whole-image URL.

    Every failed image request is appended to *failures*; *memory* passes
    to :func:`stage_service_image`.

    Raises:
        BreakerOpen, QuotaSpent, SlotLost, OSError: The download must stop.
    """
    if source.kind == "service":
        return stage_service_image(
            network,
            provider,
            source.url,
            staging_dir,
            item,
            slot=slot,
            max_px=max_px,
            failures=failures,
            memory=memory,
        )
    staged = stage_first(
        network,
        provider,
        [source.url],
        staging_dir,
        item,
        slot=slot,
        kind="image",
        failures=failures,
    )
    return staged if isinstance(staged, StagedFile) else None


def _copy_page(run: DownloadRun, staged: StagedFile, item: str) -> StagedFile:
    """Stage a copy of a staged page for another canvas with the same source.

    Each source URL is transferred once.

    Raises:
        OSError: The copy could not be written.
    """
    name = f"{item}{PurePosixPath(staged.name).suffix}"
    shutil.copyfile(run.staging_dir / staged.name, run.staging_dir / name)
    return replace(staged, name=name, item=item)


def _stage_pages(
    run: DownloadRun, canvases: list[Canvas], max_px: int | None
) -> Completeness:
    """Stage the page images within the page limit.

    Every canvas is an expected item at its manifest position. A canvas
    without a source is missing with cause ``no_source``; a failed page is
    missing with the cause of its last failed request; a canvas repeating
    an earlier canvas's source gets a copy of that page, not a transfer.
    The pages share one :class:`SizeMemory`.
    """
    limit = run.request.page_limit
    selected = canvases[:limit] if limit else canvases
    expected = tuple(page_item(canvas.index) for canvas in selected)
    present = set(expected) & run.request.present
    causes: dict[str, str] = {}
    fetched: dict[PageSource, StagedFile] = {}
    memory = SizeMemory()
    for canvas in selected:
        item = page_item(canvas.index)
        if item in present:
            continue
        if canvas.source is None:
            causes[item] = NO_SOURCE
            continue
        if run.halt is not None or not run.admits("image"):
            break
        earlier = fetched.get(canvas.source)
        if earlier is not None:
            run.keep(_copy_page(run, earlier, item))
            present.add(item)
            continue
        failures: list[RequestFailed] = []
        try:
            staged = stage_canvas(
                run.network,
                run.provider,
                canvas.source,
                run.staging_dir,
                item,
                slot=run.slot,
                max_px=max_px,
                failures=failures,
                memory=memory,
            )
        except (BreakerOpen, QuotaSpent, SlotLost) as exc:
            run.stop(exc)
            break
        if staged is None:
            logger.warning("%s: page %s could not be downloaded", run.provider, item)
            if failures:
                causes[item] = failures[-1].cause.value
            continue
        fetched[canvas.source] = staged
        run.keep(staged)
        present.add(item)
    return Completeness(
        "images",
        expected,
        tuple(item for item in expected if item in present),
        page_limit=limit,
        available=len(canvases),
        causes=causes,
    )


def download_manifest(
    network: Network,
    provider: str,
    manifest: Mapping[str, Any],
    *,
    source_id: str,
    staging_dir: Path,
    request: DownloadRequest,
    manifest_url: str = "",
    image_max_px: int | None = None,
) -> DownloadResult:
    """Stage a fetched manifest's files and report their completeness.

    The manifest itself is staged as metadata when the request includes
    metadata. ``pdf`` and ``both`` try the best rendering; ``pdf`` falls back
    to the page images when the manifest offers none or none could be
    fetched; ``images`` and ``both`` stage the page images within the page
    limit. *image_max_px* is the provider's image size cap.

    Raises:
        OSError: A file could not be written.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)
    run = DownloadRun(network, provider, staging_dir, request)
    run.stage_metadata(MANIFEST_NAME, manifest, source_url=manifest_url)
    parts: list[Completeness] = []
    renderings = select_renderings(manifest) if request.format != "images" else []
    rendering_failed = False
    if renderings:
        pdf = run.stage_rendering(renderings)
        parts.append(pdf)
        rendering_failed = not pdf.complete and run.stopped is None
    canvases = extract_canvases(manifest)
    wants_images = (
        request.format == "images"
        or (request.format == "both" and (canvases or not renderings))
        or (request.format == "pdf" and (not renderings or rendering_failed))
    )
    if wants_images:
        parts.append(_stage_pages(run, canvases, image_max_px))
    return run.result(source_id, parts)


def manifest_page_refs(
    manifest: Mapping[str, Any], *, first_n: int, max_px: int
) -> list[PageRef]:
    """Return the first *first_n* canvases as page references at *max_px*.

    A service-backed canvas gets the capped speculative sizes and its service
    base; a direct canvas its whole-image URL; a canvas without a source is
    left out.

    Raises:
        ValueError: *first_n* or *max_px* is below 1.
    """
    if first_n < 1:
        raise ValueError(f"first_n must be at least 1, got {first_n}")
    refs: list[PageRef] = []
    for canvas in extract_canvases(manifest)[:first_n]:
        source = canvas.source
        if source is None:
            continue
        if source.kind == "service":
            urls = tuple(image_url_candidates(source.url, max_px=max_px))
            service: str | None = source.url
        else:
            urls, service = (source.url,), None
        refs.append(
            PageRef(page_item(canvas.index), canvas.index, urls, canvas.label, service)
        )
    return refs
