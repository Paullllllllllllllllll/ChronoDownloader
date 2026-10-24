"""Staatsbibliothek zu Berlin digital collections: StaBiKat SRU search, METS files.

Ported from ChronoDownloader v1.30.3 ``api/providers/sbb_digital.py`` on
19.10.2026. Search queries the K10plus SRU endpoint of StaBiKat
(``sru.k10plus.de``) with up to five query variants, most specific first, and
stops at the first variant with records. Downloads read the item's METS
document from the digital collections' resolver and fetch its PDF and page
image files directly.

Changed: the SRU host, since ``sru.gbv.de`` no longer accepts connections
(24.10.2026); a failed SRU request is no longer read as "no records". A
variant the server rejects (4xx other than 404) moves on to the next one; any
other failure (a timeout above all) ends the search and is raised with its
cause, so the next variant is not sent to an unreachable host.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from chronodownloader.coord import SlotLost, SlotToken
from chronodownloader.iiif import image_url_candidates
from chronodownloader.net import BreakerOpen, Cause, Network, QuotaSpent, RequestFailed
from chronodownloader.settings import Settings

from ._file_download import wants_images
from ._query import mods_creator, mods_date, parse_year
from ._urls import on_hosts, path_match, query_value
from .base import (
    RENDERING_ITEM,
    Capabilities,
    Completeness,
    DownloadRequest,
    DownloadResult,
    FileKind,
    InvalidResponse,
    ItemNotFound,
    PageRef,
    Part,
    ProviderBlocked,
    ProviderError,
    SearchQuery,
    SearchRecord,
    StagedFile,
    StopReason,
    page_item,
    provider_error,
    source_id_of,
)
from .staging import stage_download, stage_metadata

__all__ = [
    "IMAGE_FILEGRP_PRIORITY",
    "ITEM_URL",
    "METS_URL",
    "SRU_URL",
    "SbbDigital",
    "build",
    "candidate_queries",
    "collect_mets_urls",
    "parse_sru_records",
]

logger = logging.getLogger(__name__)

KEY = "sbb_digital"
SRU_URL = "https://sru.k10plus.de/stabikat"
METS_URL = "https://digital.staatsbibliothek-berlin.de/dms/metsresolver/?PPN={ppn}"
ITEM_URL = "https://digital.staatsbibliothek-berlin.de/werkansicht?PPN={ppn}"

IMAGE_FILEGRP_PRIORITY = ("MAX", "DEFAULT", "PRESENTATION", "MIN", "THUMBS")
"""METS fileGrp/@USE values of the DFG-Viewer profile, best resolution first;
only one group is downloaded (:func:`collect_mets_urls`)."""

METS_METADATA = "mets"
"""The staged METS document's metadata name (``metadata/mets.json``)."""

_SRU_NS = {"srw": "http://www.loc.gov/zing/srw/", "mods": "http://www.loc.gov/mods/v3"}
_METS_NS = {"mets": "http://www.loc.gov/METS/"}
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"
_ACCEPT_XML = {"Accept": "application/xml"}
_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".jp2")
_IIIF_IMAGE = re.compile(r"^(?P<base>https?://.+?)/full/[^/]+/0/default\.(?:jpg|png)$")
_PPN = re.compile(r"(?:PPN)?\d+[\dxX]?")
_PPN_PATH = re.compile(r"/(PPN\d+[\dxX]?)(?:[/?#.-]|$)")


_WORD = re.compile(r"\w[\w-]*")
_DIAGNOSTIC = re.compile(rb"<(?:\w+:)?diagnostic[\s>]")
_STOPWORDS = frozenset(
    {
        *("der", "die", "das", "den", "dem", "des", "und", "oder", "ein", "eine"),
        *("einer", "eines", "von", "vom", "zur", "zum", "mit", "fur", "für", "aus"),
        *("auf", "als", "bey", "bei", "the", "and", "les", "une", "los", "las"),
        "del",
    }
)
"""Words of three letters or more left out of the title clause; shorter words
are dropped anyway."""
_MAX_TITLE_WORDS = 6


def _words(text: str) -> list[str]:
    """Return the searchable words of *text*: lowercased, stopwords and
    one- or two-letter words dropped, each once."""
    words = (word.lower() for word in _WORD.findall(text))
    kept = (word for word in words if len(word) > 2 and word not in _STOPWORDS)
    return list(dict.fromkeys(kept))


def candidate_queries(
    title: str, creator: str | None, year: int | None = None
) -> list[str]:
    """Return the CQL query variants for a work, most specific first.

    K10plus indexes words (``pica.xtit`` title, ``pica.xper`` person,
    ``pica.xjah`` year) and answers a phrase or the old ``pica.tit`` index
    with no records or a diagnostic. The variants are every title word with
    the creator's last name, every title word alone, and the two longest
    title words with the year, which still finds a record whose title is
    spelled differently from the input.
    """
    words = _words(title)[:_MAX_TITLE_WORDS]
    if not words:
        return []
    title_clause = " and ".join(f"pica.xtit={word}" for word in words)
    queries = []
    names = _words(creator or "")
    if names:
        queries.append(f"{title_clause} and pica.xper={names[-1]}")
    queries.append(title_clause)
    if year is not None and len(words) > 2:
        longest = sorted(words, key=len, reverse=True)[:2]
        loose = " and ".join(f"pica.xtit={word}" for word in longest)
        queries.append(f"{loose} and pica.xjah={year}")
    return queries


def _own_ppn(mods: ET.Element) -> str | None:
    """Return the record's own PPN, ``PPN``-prefixed, or None.

    Direct children only: ``.//`` descends into ``<relatedItem
    type="host">``, whose recordInfo names the series, so a volume was
    downloaded under the series PPN. An identifier whose ``source`` names a
    PPN wins over the first identifier.
    """
    record_id = None
    for rec_id in mods.findall("mods:recordInfo/mods:recordIdentifier", _SRU_NS):
        if not rec_id.text:
            continue
        if "ppn" in (rec_id.get("source") or "").lower():
            record_id = rec_id.text.strip()
            break
        if not record_id:
            record_id = rec_id.text.strip()
    if not record_id:
        return None
    return record_id if record_id.upper().startswith("PPN") else f"PPN{record_id}"


def parse_sru_records(
    xml: bytes | str, *, limit: int, mets_url: str = METS_URL
) -> list[SearchRecord]:
    """Return at most *limit* records of a StaBiKat SRU response (MODS schema).

    Records without an own PPN are skipped: they cannot be resolved.

    Raises:
        xml.etree.ElementTree.ParseError: The body is not XML.
    """
    root = ET.fromstring(xml)
    records: list[SearchRecord] = []
    for record in root.findall(".//srw:record", _SRU_NS):
        mods = record.find(".//mods:mods", _SRU_NS)
        if mods is None:
            continue
        ppn = _own_ppn(mods)
        if ppn is None:
            continue
        title_el = mods.find(".//mods:titleInfo/mods:title", _SRU_NS)
        subtitle_el = mods.find(".//mods:titleInfo/mods:subTitle", _SRU_NS)
        title = title_el.text.strip() if title_el is not None and title_el.text else ""
        if subtitle_el is not None and subtitle_el.text:
            title = f"{title} {subtitle_el.text.strip()}"
        creator = mods_creator(mods, _SRU_NS)
        date = mods_date(mods, _SRU_NS)
        item_url = ITEM_URL.format(ppn=ppn)
        records.append(
            SearchRecord(
                provider=KEY,
                source_id=ppn,
                title=title,
                creators=(creator,) if creator else (),
                date=date,
                year=parse_year(date),
                item_url=item_url,
                raw={"mets_url": mets_url.format(ppn=ppn), "item_url": item_url},
            )
        )
        if len(records) >= limit:
            break
    return records


def collect_mets_urls(mets_xml: bytes | str) -> tuple[list[str], list[str]]:
    """Return the PDF URLs and the page image URLs of a METS document.

    A METS file section holds one ``fileGrp`` per derivative (``THUMBS``,
    ``MIN``, ``DEFAULT``, ``MAX`` in the DFG-Viewer profile), each with one
    file per page; collecting across all of them fetched every page several
    times. The images come from one group, chosen by
    :data:`IMAGE_FILEGRP_PRIORITY`; unlabeled or unknown groups fall back to
    every image found. PDFs are collected across all groups, once per href.

    Raises:
        xml.etree.ElementTree.ParseError: The document is not XML.
    """
    root = ET.fromstring(mets_xml)
    pdf_urls: list[str] = []
    images_by_group: dict[str, list[str]] = {}
    for grp in root.findall(".//mets:fileGrp", _METS_NS):
        use = (grp.get("USE") or "").upper()
        for file_el in grp.findall("mets:file", _METS_NS):
            mimetype = (file_el.get("MIMETYPE") or "").lower()
            flocat = file_el.find("mets:FLocat", _METS_NS)
            href = flocat.get(_XLINK_HREF) if flocat is not None else None
            if not href:
                continue
            href_lower = href.lower()
            if "pdf" in mimetype or href_lower.endswith(".pdf"):
                pdf_urls.append(href)
            elif "image" in mimetype or href_lower.endswith(_IMAGE_SUFFIXES):
                images_by_group.setdefault(use, []).append(href)
    pdf_urls = list(dict.fromkeys(pdf_urls))
    for use in IMAGE_FILEGRP_PRIORITY:
        if images_by_group.get(use):
            return pdf_urls, images_by_group[use]
    return pdf_urls, [url for urls in images_by_group.values() for url in urls]


def _ppn(value: str) -> str:
    value = value.strip()
    if not value:
        raise ItemNotFound(KEY, value)
    return value if value.upper().startswith("PPN") else f"PPN{value}"


def _search_failure(error: RequestFailed | BreakerOpen) -> ProviderError:
    if isinstance(error, RequestFailed):
        return ProviderBlocked(KEY, error.cause.value, detail=str(error))
    return provider_error(KEY, error)


def _halt(error: BreakerOpen | QuotaSpent | SlotLost) -> StopReason:
    if isinstance(error, BreakerOpen):
        return "breaker_open"
    if isinstance(error, QuotaSpent):
        return "quota_spent"
    return "slot_lost"


def _stage_items(
    network: Network,
    entries: Sequence[tuple[str, str]],
    staging_dir: Path,
    request: DownloadRequest,
    kind: FileKind,
    files: list[StagedFile],
) -> tuple[tuple[str, ...], StopReason | None]:
    """Stage each (item, URL) entry; return the present items and the stop.

    Items the request names as present are skipped and count as present; a
    failed file is missing and the next one is tried; the size budget, an
    open breaker, a spent quota or a lost slot ends the loop.
    """
    present: list[str] = []
    for item, url in entries:
        if item in request.present:
            present.append(item)
            continue
        if request.budget is not None and not request.budget.allows(kind):
            logger.warning("%s: size limit reached for %s files", KEY, kind)
            return tuple(present), "budget"
        try:
            staged = stage_download(
                network, KEY, url, staging_dir, item, slot=request.slot, kind=kind
            )
        except RequestFailed as exc:
            logger.warning("%s: %s failed: %s", KEY, item, exc)
            continue
        except (BreakerOpen, QuotaSpent, SlotLost) as exc:
            logger.warning("%s: download stopped: %s", KEY, exc)
            return tuple(present), _halt(exc)
        files.append(staged)
        if request.budget is not None:
            request.budget.charge(kind, staged.size)
        present.append(item)
    return tuple(present), None


def _stage_part(  # noqa: PLR0913 - the download's state, passed through
    network: Network,
    part: Part,
    entries: Sequence[tuple[str, str]],
    available: int,
    staging_dir: Path,
    request: DownloadRequest,
    files: list[StagedFile],
    *,
    stopped: StopReason | None = None,
) -> tuple[Completeness, StopReason | None]:
    """Stage the (item, URL) *entries* of *part* and return its completeness.

    After an earlier stop nothing is fetched: only the items the request
    names as present count. *available* is the number of files the METS
    document lists for the part before the page limit.
    """
    if stopped is None:
        kind: FileKind = "pdf" if part == "pdf" else "image"
        present, stopped = _stage_items(
            network, entries, staging_dir, request, kind, files
        )
    else:
        present = tuple(item for item, _url in entries if item in request.present)
    completeness = Completeness(
        part,
        tuple(item for item, _url in entries),
        present,
        page_limit=request.page_limit,
        available=available,
    )
    return completeness, stopped


def _stage_mets(
    staging_dir: Path,
    request: DownloadRequest,
    mets_text: str,
    mets_url: str,
    files: list[StagedFile],
) -> None:
    """Stage the METS document as metadata when the request and budget allow."""
    budget = request.budget
    if not request.include_metadata or (
        budget is not None and not budget.allows("metadata")
    ):
        return
    staged = stage_metadata(
        staging_dir, METS_METADATA, {"mets_xml": mets_text}, source_url=mets_url
    )
    files.append(staged)
    if budget is not None:
        budget.charge("metadata", staged.size)


def _limited(urls: list[str], limit: int | None) -> list[str]:
    """Return the first *limit* URLs, or all of them without a limit."""
    return urls[:limit] if limit else urls


def _pdf_items(count: int) -> list[str]:
    if count == 1:
        return [RENDERING_ITEM]
    return [f"{RENDERING_ITEM}_{index:05d}" for index in range(1, count + 1)]


@dataclass(frozen=True)
class SbbDigital:
    """The SBB adapter; endpoint URLs are fields so tests can point them away.

    ``source_id`` is the record's own PPN, ``PPN``-prefixed. A METS file
    group of PDFs is the item's rendering: one PDF is the ``rendering`` item,
    several (one per page) are ``rendering_00001`` and so on and, as in
    v1.30.3, the page limit applies to them as to the page images.
    """

    hosts: ClassVar[frozenset[str]] = frozenset(
        {"staatsbibliothek-berlin.de", "sru.k10plus.de"}
    )
    sru_url: str = SRU_URL
    mets_url: str = METS_URL

    @property
    def key(self) -> str:
        return KEY

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities()

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        """Return the records of the first query variant that has any.

        Raises:
            ProviderBlocked: A request failed for a cause other than a
                rejected variant (timeouts as ``transient``), the breaker is
                open, or every variant was rejected.
            InvalidResponse: The endpoint answered with something not XML.
        """
        if limit < 1:
            return []
        rejected: RequestFailed | None = None
        answered = False
        for cql in candidate_queries(query.title, query.creator, query.year):
            params = {
                "version": "1.2",
                "operation": "searchRetrieve",
                "query": cql,
                "maximumRecords": str(limit),
                "recordSchema": "mods",
            }
            try:
                response = network.request(
                    KEY, "GET", self.sru_url, params=params, headers=_ACCEPT_XML
                )
            except RequestFailed as exc:
                if exc.cause is not Cause.REJECTED:
                    raise _search_failure(exc) from exc
                logger.info("%s: query variant rejected: %s", KEY, exc)
                rejected = exc
                continue
            except BreakerOpen as exc:
                raise _search_failure(exc) from exc
            answered = True
            try:
                records = parse_sru_records(
                    response.content, limit=limit, mets_url=self.mets_url
                )
            except ET.ParseError as exc:
                raise InvalidResponse(KEY, self.sru_url, f"not XML: {exc}") from exc
            if records:
                return records
            if _DIAGNOSTIC.search(response.content):
                logger.warning("%s: SRU diagnostic for query %s", KEY, cql)
        if not answered and rejected is not None:
            raise _search_failure(rejected) from rejected
        return []

    def _mets(
        self, network: Network, record_or_id: SearchRecord | str, slot: SlotToken | None
    ) -> tuple[str, str, str, list[str], list[str]]:
        """Return the PPN, METS URL, METS text, PDF URLs and image URLs.

        Raises:
            ItemNotFound: No identifier, or the resolver answered 404.
            ProviderBlocked: The request failed otherwise.
            InvalidResponse: The METS document is not XML.
        """
        ppn = _ppn(source_id_of(record_or_id))
        url = self.mets_url.format(ppn=ppn)
        if isinstance(record_or_id, SearchRecord):
            url = str(record_or_id.raw.get("mets_url") or url)
        try:
            response = network.request(KEY, "GET", url, headers=_ACCEPT_XML, slot=slot)
        except (RequestFailed, BreakerOpen, QuotaSpent, SlotLost) as exc:
            raise provider_error(KEY, exc, ppn) from exc
        try:
            pdf_urls, image_urls = collect_mets_urls(response.content)
        except ET.ParseError as exc:
            raise InvalidResponse(KEY, url, f"METS is not XML: {exc}") from exc
        return ppn, url, response.text, pdf_urls, image_urls

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        """Stage the item's PDFs and page images from its METS document.

        ``pdf`` takes the PDFs, falling back to the page images when there
        are none or one could not be fetched; ``images`` takes the page
        images; ``both`` takes both.

        Raises:
            ItemNotFound, ProviderBlocked, InvalidResponse: The METS document
                could not be had.
            OSError: A file could not be written.
        """
        ppn, mets_url, mets_text, pdf_urls, image_urls = self._mets(
            network, record_or_id, request.slot
        )
        staging_dir.mkdir(parents=True, exist_ok=True)
        files: list[StagedFile] = []
        _stage_mets(staging_dir, request, mets_text, mets_url, files)
        limit = request.page_limit
        parts: list[Completeness] = []
        stopped: StopReason | None = None
        pdf_failed = False
        if request.format != "images" and pdf_urls:
            entries = list(
                zip(_pdf_items(len(pdf_urls)), _limited(pdf_urls, limit), strict=False)
            )
            pdf, stopped = _stage_part(
                network, "pdf", entries, len(pdf_urls), staging_dir, request, files
            )
            parts.append(pdf)
            pdf_failed = not pdf.complete and stopped is None
        if wants_images(
            request.format,
            offered=bool(pdf_urls),
            rendering_failed=pdf_failed,
            has_pages=bool(image_urls),
        ):
            entries = [
                (page_item(index), url)
                for index, url in enumerate(_limited(image_urls, limit), start=1)
            ]
            images, stopped = _stage_part(
                network,
                "images",
                entries,
                len(image_urls),
                staging_dir,
                request,
                files,
                stopped=stopped,
            )
            parts.append(images)
        logger.info("%s: staged %d file(s) for %s", KEY, len(files), ppn)
        return DownloadResult(
            provider=KEY,
            source_id=ppn,
            format=request.format,
            parts=tuple(parts),
            files=tuple(files),
            stopped=stopped,
        )

    def pages(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        first_n: int,
        max_px: int,
    ) -> list[PageRef]:
        """Return the first *first_n* page images of the METS document.

        An image URL of the IIIF Image API shape (``.../full/<size>/0/
        default.jpg``) is given at the reduced size, with its service base;
        any other URL as it stands.

        Raises:
            ValueError: *first_n* is below 1.
            ItemNotFound, ProviderBlocked, InvalidResponse: As
                :meth:`download`.
        """
        if first_n < 1:
            raise ValueError(f"first_n must be at least 1, got {first_n}")
        *_, image_urls = self._mets(network, record_or_id, None)
        refs: list[PageRef] = []
        for index, url in enumerate(image_urls[:first_n], start=1):
            match = _IIIF_IMAGE.match(url)
            if match is None:
                refs.append(PageRef(page_item(index), index, (url,)))
                continue
            base = match.group("base")
            urls = tuple(image_url_candidates(base, max_px=max_px))
            refs.append(PageRef(page_item(index), index, urls, service=base))
        return refs

    def item_id_from_url(self, url: str) -> str | None:
        """Return the ``PPN``-prefixed PPN of a viewer or content URL
        (``werkansicht?PPN=PPN...``, ``.../dc/PPN.../...``)."""
        if not on_hosts(url, self.hosts):
            return None
        found = query_value(url, "PPN") or path_match(url, _PPN_PATH)
        if not found or not _PPN.fullmatch(found):
            return None
        return found if found.startswith("PPN") else f"PPN{found}"


def build(settings: Settings) -> SbbDigital:
    """Return the SBB adapter; it reads nothing from *settings*."""
    return SbbDigital()
