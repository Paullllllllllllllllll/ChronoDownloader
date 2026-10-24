"""Biblioteca Nacional de España: datos.bne.es SPARQL search, BNE Digital PDFs.

Ported from ChronoDownloader v1.30.3 ``api/providers/bne.py`` on 19.10.2026.
Two BNE services are involved, with different identifier namespaces:

* ``datos.bne.es``, the linked-data catalogue, queried over SPARQL. Its
  resource identifiers cover authority records (``XX...``) as well as
  bibliographic ones (``a...``, ``bimo...``).
* ``bnedigital.bne.es``, BNE Digital, the digitisation platform that
  superseded the Biblioteca Digital Hispánica in 2025. Its objects are
  addressed by UUID and delivered as PDF page ranges; it publishes no IIIF
  manifests, and the old host ``iiif.bne.es`` no longer resolves.

The bridge is the catalogue record's link to the BNE Digital card URL that
carries the UUID (:data:`DIGITAL_LINK_PATH`). Search keeps bibliographic
manifestations (``C1003``) with such a link; downloads work in the BNE
Digital namespace only.

Changed: a refused or failed SPARQL request is no longer read as "no
records"; it raises with its cause. datos.bne.es sits behind Cloudflare,
which refuses the requests client with a 403; the built-in profile
impersonates Chrome.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from chronodownloader.coord import SlotLost, SlotToken
from chronodownloader.net import BreakerOpen, Network, QuotaSpent, RequestFailed
from chronodownloader.settings import Settings

from ._query import escape_sparql_string, parse_year
from ._urls import on_hosts
from .base import (
    RENDERING_ITEM,
    Capabilities,
    Completeness,
    DownloadRequest,
    DownloadResult,
    InvalidResponse,
    ItemNotFound,
    PageRef,
    ProviderBlocked,
    SearchQuery,
    SearchRecord,
    StagedFile,
    StopReason,
    UnsupportedCapability,
    provider_error,
    source_id_of,
)
from .staging import stage_download, stage_metadata

__all__ = [
    "ACCENT_FOLDING",
    "CHUNK_LIMIT",
    "DIGITAL_BASE_URL",
    "DIGITAL_PDF_URL",
    "END_OF_WORK_STATUS",
    "END_UNVERIFIED",
    "HISTORICAL_YEAR_PATTERN",
    "MAX_PDF_CHUNKS",
    "PDF_PAGE_CHUNK",
    "SEARCH_API_URL",
    "Bne",
    "build",
    "build_search_query",
    "catalog_resource_id",
    "chunk_item",
    "extract_digital_id",
    "fold_accents",
    "parse_search_bindings",
]

logger = logging.getLogger(__name__)

KEY = "bne"
SEARCH_API_URL = "https://datos.bne.es/sparql"
DIGITAL_BASE_URL = "https://bnedigital.bne.es"
DIGITAL_CARD_URL = DIGITAL_BASE_URL + "/bd/card?id={item_id}"
DIGITAL_PDF_URL = DIGITAL_BASE_URL + "/bd/es/pdf"

MANIFESTATION_CLASS = "https://datos.bne.es/def/C1003"
CREATOR_PROPERTY = "https://datos.bne.es/def/P1011"
DATE_PROPERTY = "https://datos.bne.es/def/P3006"
LABEL_PROPERTY = "http://www.w3.org/2000/01/rdf-schema#label"
SEE_ALSO_PROPERTY = "http://www.w3.org/2000/01/rdf-schema#seeAlso"
DIGITAL_COPY_PROPERTY = "https://datos.bne.es/def/OP3001"
DIGITAL_LINK_PATH = f"<{DIGITAL_COPY_PROPERTY}>|<{SEE_ALSO_PROPERTY}>"
"""The SPARQL path to a record's BNE Digital card: ``OP3001`` on the current
catalogue (24.10.2026), ``rdfs:seeAlso`` on the about 1,400 records that
still carry only the older link."""

PDF_PAGE_CHUNK = 25
"""BNE Digital answers a PDF range of more than 25 pages (the viewer's
``downloadPageLimit``) with HTTP 500, so works are fetched in 25-page ranges."""

MAX_PDF_CHUNKS = 400
"""A safety stop on the range loop, not an expected limit."""

END_OF_WORK_STATUS = 500
"""BNE Digital answers a range that starts past the last page with HTTP 500:
for this provider a 500 on a range marks the end of the work, not a failure.
The range request names it as a final status, so it is neither retried nor
counted on the breaker. A 500 is no proof of the end, though: see
:data:`END_UNVERIFIED`."""

END_UNVERIFIED = "end_unverified"
"""The cause of the range answered with :data:`END_OF_WORK_STATUS`: the work
probably ended before it, but nothing proves it."""

CHUNK_LIMIT = "chunk_limit"
"""The cause of the range after :data:`MAX_PDF_CHUNKS` ranges."""

METADATA_NAME = "item"
"""The staged item metadata's name (``metadata/item.json``)."""

ACCENT_FOLDING: tuple[tuple[str, str], ...] = (
    ("á", "a"),
    ("é", "e"),
    ("í", "i"),
    ("ó", "o"),
    ("ú", "u"),
    ("ü", "u"),
    ("ñ", "n"),
)
"""Spanish orthography's seven non-ASCII letters. The endpoint has no
accent-insensitive comparison, so both sides of the title filter are folded
through this table: the query in Python, the title by a SPARQL REPLACE
chain."""

HISTORICAL_YEAR_PATTERN = "(1[0-8][0-9][0-9]|1900)"
"""A year in 1000-1900. The date property is free text ("1845", "[1770]",
"1886-1945", "S.XVIII", "A. MDCCLXII"), so the filter is a membership test
that fails open on purpose for undated records and for dates without a
four-digit year (the oldest material). Extracting and comparing the year
instead parses on the endpoint but is refused by its WAF with a 403."""

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# A datos.bne.es resource id: a letter prefix plus digits, optionally after a
# parenthesised MARC organization code ("(CaPaEBR)a6232137"). Narrow on
# purpose: the id goes unescaped into the seeAlso query.
_CATALOG_ID_RE = re.compile(r"^(?:\([A-Za-z]{1,10}\))?[A-Za-z]{0,4}\d[A-Za-z0-9]*$")


def _bindings(document: Any) -> list[Mapping[str, Any]]:
    results = document.get("results") if isinstance(document, Mapping) else None
    bindings = results.get("bindings") if isinstance(results, Mapping) else None
    if not isinstance(bindings, list):
        return []
    return [binding for binding in bindings if isinstance(binding, Mapping)]


def _binding_value(binding: Mapping[str, Any], name: str) -> str | None:
    cell = binding.get(name)
    if not isinstance(cell, Mapping):
        return None
    value = cell.get("value")
    return str(value) if value else None


def extract_digital_id(value: str | None) -> str | None:
    """Return the lowercase BNE Digital UUID in *value* (a UUID or any BNE
    Digital URL carrying one), or None."""
    if not value:
        return None
    match = _UUID_RE.search(str(value))
    return match.group(0).lower() if match else None


def catalog_resource_id(value: str) -> str | None:
    """Return the bare datos.bne.es resource id of a URI or id, or None when
    it is foreign or unsafe to put into a query."""
    candidate = str(value).strip()
    if candidate.startswith("http"):
        if "datos.bne.es" not in candidate:
            return None
        candidate = candidate.rstrip("/").rsplit("/", 1)[-1]
    return candidate if _CATALOG_ID_RE.match(candidate) else None


def fold_accents(value: str) -> str:
    """Lowercase *value* and map the Spanish accented letters onto ASCII."""
    folded = value.lower()
    for accented, plain in ACCENT_FOLDING:
        folded = folded.replace(accented, plain)
    return folded


def _folded_sparql_expression(expression: str) -> str:
    """Wrap a SPARQL expression in the fold :func:`fold_accents` applies."""
    folded = f"LCASE({expression})"
    for accented, plain in ACCENT_FOLDING:
        folded = f"REPLACE({folded}, '{accented}', '{plain}')"
    return folded


def build_search_query(title: str, limit: int) -> str:
    """Return the SPARQL query for digitised BNE editions matching *title*."""
    t = escape_sparql_string(fold_accents(title))
    folded_title = _folded_sparql_expression("?title")
    return f"""
        SELECT DISTINCT ?id ?title ?digital ?creator ?date WHERE {{
            ?id a <{MANIFESTATION_CLASS}> .
            ?id <{LABEL_PROPERTY}> ?title .
            ?id {DIGITAL_LINK_PATH} ?digital .
            FILTER(CONTAINS({folded_title}, '{t}'))
            FILTER(STRSTARTS(STR(?digital), '{DIGITAL_BASE_URL}/'))
            OPTIONAL {{ ?id <{CREATOR_PROPERTY}> ?creator . }}
            OPTIONAL {{ ?id <{DATE_PROPERTY}> ?date . }}
            FILTER(
                !BOUND(?date)
                || !REGEX(STR(?date), '[0-9]{{4}}')
                || REGEX(STR(?date), '{HISTORICAL_YEAR_PATTERN}')
            )
        }} LIMIT {limit}
    """


def _see_also_query(resource_id: str) -> str:
    return f"""
        SELECT ?digital WHERE {{
            <https://datos.bne.es/resource/{resource_id}>
                {DIGITAL_LINK_PATH} ?digital .
            FILTER(STRSTARTS(STR(?digital), '{DIGITAL_BASE_URL}/'))
        }} LIMIT 1
    """


def parse_search_bindings(document: Any, *, limit: int) -> list[SearchRecord]:
    """Return at most *limit* distinct editions of a search result.

    A binding without a BNE Digital UUID is dropped; repeated UUIDs (one
    binding per OPTIONAL combination) are kept once.
    """
    records: list[SearchRecord] = []
    seen: set[str] = set()
    for binding in _bindings(document):
        digital_url = _binding_value(binding, "digital")
        digital_id = extract_digital_id(digital_url)
        if not digital_id or digital_id in seen:
            continue
        seen.add(digital_id)
        creator = _binding_value(binding, "creator")
        date = _binding_value(binding, "date")
        records.append(
            SearchRecord(
                provider=KEY,
                source_id=digital_id,
                title=_binding_value(binding, "title") or "",
                creators=(creator,) if creator else (),
                date=date,
                year=parse_year(date),
                item_url=digital_url,
                raw={"catalog_id": _binding_value(binding, "id")},
            )
        )
        if len(records) >= limit:
            break
    return records


def chunk_item(start: int, end: int) -> str:
    """Return the item of the PDF range from page *start* to *end*."""
    return f"{RENDERING_ITEM}_p{start:05d}_{end:05d}"


def _chunks(limit: int | None) -> list[tuple[int, int]]:
    """Return the page ranges to try, at most :data:`MAX_PDF_CHUNKS`."""
    ranges: list[tuple[int, int]] = []
    start = 1
    while len(ranges) < MAX_PDF_CHUNKS and not (limit and start > limit):
        end = start + PDF_PAGE_CHUNK - 1
        if limit:
            end = min(end, limit)
        ranges.append((start, end))
        start = end + 1
    return ranges


def _halt(error: BreakerOpen | QuotaSpent | SlotLost) -> StopReason:
    if isinstance(error, BreakerOpen):
        return "breaker_open"
    if isinstance(error, QuotaSpent):
        return "quota_spent"
    return "slot_lost"


@dataclass(frozen=True)
class Bne:
    """The BNE adapter; endpoint URLs are fields so tests can point them away.

    ``source_id`` is the BNE Digital UUID, the only downloadable identifier;
    the catalogue record's URI stays in ``raw["catalog_id"]``. ``download``
    also accepts a datos.bne.es id and resolves it through ``rdfs:seeAlso``.

    BNE Digital serves no page images: an item is delivered as consecutive
    PDF ranges of :data:`PDF_PAGE_CHUNK` pages (items ``rendering_p00001_
    00025`` and so on), reported as the ``pdf`` part for ``pdf`` and
    ``both``. The page limit bounds the page ranges, as in v1.30.3; with a
    page limit, the work is complete once every range within it arrived.
    BNE publishes no page count the adapter can read, so without a limit
    the end of a work is never proven: the first range answered with HTTP
    500 (:data:`END_OF_WORK_STATUS`) ends the loop and stays expected,
    missing with cause :data:`END_UNVERIFIED`, and exhausting
    :data:`MAX_PDF_CHUNKS` leaves the next range missing with cause
    :data:`CHUNK_LIMIT`; either way the work stays partial. Any other
    failure ends the loop with that range missing. ``available`` counts the
    ranges that exist up to where the loop ended.
    """

    hosts: ClassVar[frozenset[str]] = frozenset({"bne.es"})
    sparql_url: str = SEARCH_API_URL
    pdf_url: str = DIGITAL_PDF_URL

    @property
    def key(self) -> str:
        return KEY

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities()

    def _sparql(
        self, network: Network, query: str, *, slot: SlotToken | None = None
    ) -> Any:
        """Send a SPARQL query and return the decoded JSON document.

        Raises:
            RequestFailed, BreakerOpen, QuotaSpent, SlotLost: As
                :meth:`Network.request`.
            InvalidResponse: The body is not JSON.
        """
        response = network.request(
            KEY,
            "GET",
            self.sparql_url,
            params={"query": query, "format": "json"},
            slot=slot,
        )
        try:
            return response.json()
        except ValueError as exc:
            raise InvalidResponse(KEY, self.sparql_url, "not JSON") from exc

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        """Return digitised editions whose title contains the query's title.

        The creator is not put into the query (BNE names authors both as
        literals and as authority links); matching weighs it later. The
        query asks for four times *limit* bindings, since OPTIONAL clauses
        repeat a record per binding.

        Raises:
            ProviderBlocked: The request failed (``blocked`` for the 403 of
                datos.bne.es) or the breaker is open.
            InvalidResponse: The endpoint answered with something not JSON.
        """
        if limit < 1:
            return []
        logger.info("%s: searching for %r", KEY, query.title)
        try:
            document = self._sparql(network, build_search_query(query.title, limit * 4))
        except RequestFailed as exc:
            raise ProviderBlocked(KEY, exc.cause.value, detail=str(exc)) from exc
        except BreakerOpen as exc:
            raise provider_error(KEY, exc) from exc
        return parse_search_bindings(document, limit=limit)

    def _digital_id(self, network: Network, value: str, slot: SlotToken | None) -> str:
        """Map a BNE identifier onto a BNE Digital UUID.

        A UUID, or a BNE Digital URL holding one, is used as it is; a
        datos.bne.es id is looked up through its ``rdfs:seeAlso`` link.

        Raises:
            ItemNotFound: *value* holds neither, or the record links to no
                digital copy (authority records, undigitised editions).
            ProviderBlocked: The lookup failed.
            InvalidResponse: The lookup answered with something not JSON.
        """
        digital_id = extract_digital_id(value)
        if digital_id:
            return digital_id
        resource_id = catalog_resource_id(value)
        if not resource_id:
            raise ItemNotFound(KEY, value)
        logger.info("%s: resolving catalogue record %s", KEY, resource_id)
        try:
            document = self._sparql(network, _see_also_query(resource_id), slot=slot)
        except (RequestFailed, BreakerOpen, QuotaSpent, SlotLost) as exc:
            raise provider_error(KEY, exc, value) from exc
        for binding in _bindings(document):
            digital_id = extract_digital_id(_binding_value(binding, "digital"))
            if digital_id:
                return digital_id
        raise ItemNotFound(KEY, value)

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        """Stage the item as PDF page ranges until the end of the work.

        Raises:
            UnsupportedCapability: The request asks for page images only.
            ItemNotFound, ProviderBlocked, InvalidResponse: The identifier
                could not be resolved.
            OSError: A file could not be written.
        """
        if request.format == "images":
            raise UnsupportedCapability(KEY, "images")
        item_id = source_id_of(record_or_id).strip()
        digital_id = self._digital_id(network, item_id, request.slot)
        staging_dir.mkdir(parents=True, exist_ok=True)
        files: list[StagedFile] = []
        budget = request.budget
        if request.include_metadata and (budget is None or budget.allows("metadata")):
            record = record_or_id if isinstance(record_or_id, SearchRecord) else None
            staged = stage_metadata(
                staging_dir,
                METADATA_NAME,
                {
                    "digital_id": digital_id,
                    "item_url": DIGITAL_CARD_URL.format(item_id=digital_id),
                    "title": record.title if record else None,
                    "catalog_id": record.raw.get("catalog_id") if record else None,
                },
            )
            files.append(staged)
            if budget is not None:
                budget.charge("metadata", staged.size)
        part, stopped = self._stage_ranges(
            network, digital_id, staging_dir, request, files
        )
        return DownloadResult(
            provider=KEY,
            source_id=digital_id,
            format=request.format,
            parts=(part,),
            files=tuple(files),
            stopped=stopped,
        )

    def _stage_ranges(
        self,
        network: Network,
        digital_id: str,
        staging_dir: Path,
        request: DownloadRequest,
        files: list[StagedFile],
    ) -> tuple[Completeness, StopReason | None]:
        """Stage the page ranges; return the ``pdf`` part and the stop."""
        expected: list[str] = []
        present: list[str] = []
        causes: dict[str, str] = {}

        def part() -> Completeness:
            return Completeness(
                "pdf",
                tuple(expected),
                tuple(present),
                page_limit=request.page_limit,
                available=len(expected),
                causes=causes,
            )

        chunks = _chunks(request.page_limit)
        for start, end in chunks:
            item = chunk_item(start, end)
            if item in request.present:
                expected.append(item)
                present.append(item)
                continue
            if request.budget is not None and not request.budget.allows("pdf"):
                logger.warning("%s: size limit reached for pdf files", KEY)
                expected.append(item)
                return part(), "budget"
            page_range = str(start) if start == end else f"{start}-{end}"
            url = f"{self.pdf_url}?id={digital_id}&page={page_range}"
            try:
                staged = stage_download(
                    network,
                    KEY,
                    url,
                    staging_dir,
                    item,
                    slot=request.slot,
                    kind="pdf",
                    final_statuses=frozenset({END_OF_WORK_STATUS}),
                )
            except RequestFailed as exc:
                expected.append(item)
                if exc.status == END_OF_WORK_STATUS:
                    logger.info("%s: pages %s probably past the end", KEY, page_range)
                    causes[item] = END_UNVERIFIED
                else:
                    logger.warning("%s: pages %s failed: %s", KEY, page_range, exc)
                    causes[item] = exc.cause.value
                return part(), None
            except (BreakerOpen, QuotaSpent, SlotLost) as exc:
                logger.warning("%s: download stopped: %s", KEY, exc)
                expected.append(item)
                return part(), _halt(exc)
            files.append(staged)
            if request.budget is not None:
                request.budget.charge("pdf", staged.size)
            expected.append(item)
            present.append(item)
        limit = request.page_limit
        last_end = chunks[-1][1] if chunks else 0
        if len(chunks) == MAX_PDF_CHUNKS and not (limit and last_end >= limit):
            start = last_end + 1
            end = start + PDF_PAGE_CHUNK - 1
            item = chunk_item(start, min(end, limit) if limit else end)
            logger.warning("%s: %d ranges reached; stopping", KEY, MAX_PDF_CHUNKS)
            expected.append(item)
            causes[item] = CHUNK_LIMIT
        return part(), None

    def pages(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        first_n: int,
        max_px: int,
    ) -> list[PageRef]:
        """BNE Digital serves no page images.

        Raises:
            UnsupportedCapability: Always.
        """
        raise UnsupportedCapability(KEY, "pages")

    def item_id_from_url(self, url: str) -> str | None:
        """Return the UUID of a BNE Digital card or viewer URL
        (``bnedigital.bne.es/bd/card?id=<uuid>``, ``.../viewer?id=<uuid>``)."""
        if not on_hosts(url, ("bnedigital.bne.es",)):
            return None
        return extract_digital_id(url)


def build(settings: Settings) -> Bne:
    """Return the BNE adapter; it reads nothing from *settings*."""
    return Bne()
