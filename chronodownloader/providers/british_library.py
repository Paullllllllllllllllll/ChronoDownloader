"""British Library: SRU search with a BNB SPARQL fallback, IIIF downloads.

Ported from ChronoDownloader v1.30.3 ``api/providers/british_library.py`` on
19.10.2026. Search asks the BL SRU endpoint for Dublin Core records and
falls back to the BNB SPARQL endpoint when SRU yields no record with a BL
ARK. A download fetches the item's IIIF manifest; when the manifest
endpoint fails or answers with something other than a JSON object (api.bl.uk
serves error pages with status 200), the manifest URL is looked up in the
public viewer page.

Changed: a failed request is no longer read as "no records". An SRU failure
still leads to the SPARQL fallback, but when the fallback finds nothing or
fails too, the search raises the failure with its cause (``dns`` for the
unresolvable ``sru.bl.uk``, ``transient`` for a timeout), naming both
endpoints' failures in the detail.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from chronodownloader.coord import SlotLost, SlotToken
from chronodownloader.net import BreakerOpen, Network, QuotaSpent, RequestFailed
from chronodownloader.settings import Settings

from ._query import escape_sparql_string, escape_sru_literal, parse_year
from ._urls import on_hosts, path_match
from .base import (
    Capabilities,
    DownloadRequest,
    DownloadResult,
    InvalidResponse,
    ItemNotFound,
    PageRef,
    ProviderBlocked,
    ProviderError,
    SearchQuery,
    SearchRecord,
    provider_error,
    source_id_of,
)
from .iiif_download import download_manifest, fetch_json, manifest_page_refs

__all__ = [
    "BNB_SPARQL_URL",
    "MANIFEST_URL",
    "SRU_URL",
    "VIEWER_URL",
    "BritishLibrary",
    "build",
    "build_bnb_query",
    "parse_bnb_bindings",
    "parse_sru_records",
]

logger = logging.getLogger(__name__)

KEY = "british_library"
SRU_URL = "https://sru.bl.uk/SRU"
MANIFEST_URL = "https://api.bl.uk/metadata/iiif/ark:/81055/{identifier}/manifest.json"
BNB_SPARQL_URL = "https://bnb.data.bl.uk/sparql"
VIEWER_URL = "https://access.bl.uk/item/viewer/ark:/81055/{identifier}"

_SRU_NS = {
    "srw": "http://www.loc.gov/zing/srw/",
    "dc": "http://purl.org/dc/elements/1.1/",
}
_SRU_ARK = re.compile(r"ark:/81055/([^\s/?#]+)")
_BNB_ARK = re.compile(r"ark:/81055/([^\s\"'<>]+)")
_VIEWER_MANIFEST = re.compile(r"https?://[^\"'<>]+/manifest\.json")
_HALTS = frozenset({"breaker_open", "quota_spent", "slot_lost"})


def _text(element: ET.Element | None) -> str | None:
    return element.text if element is not None else None


def parse_sru_records(xml: bytes | str, *, limit: int) -> list[SearchRecord]:
    """Return at most *limit* records of a BL SRU response with a BL ARK.

    ``recordData`` carries one container element (``srw_dc:dc`` or the
    OAI-PMH ``oai_dc:dc``); the ``dc:`` children are read from whichever it
    is. The ARK stops at the first separator, so a manifest URL or a prose
    gloss does not leak into it. A record without an ARK cannot be
    downloaded and is skipped.

    Raises:
        xml.etree.ElementTree.ParseError: The body is not XML.
    """
    root = ET.fromstring(xml)
    records: list[SearchRecord] = []
    for record_data in root.findall(".//srw:recordData", _SRU_NS):
        container = next(iter(record_data), None)
        if container is None:
            continue
        title = _text(container.find("dc:title", _SRU_NS))
        identifier = _text(container.find("dc:identifier", _SRU_NS))
        match = _SRU_ARK.search(identifier or "")
        if match is None:
            logger.debug("%s: skipping SRU record without an ARK (%s)", KEY, title)
            continue
        creator = _text(container.find("dc:creator", _SRU_NS))
        date = _text(container.find("dc:date", _SRU_NS))
        records.append(
            SearchRecord(
                provider=KEY,
                source_id=match.group(1),
                title=title or "",
                creators=(creator,) if creator else (),
                date=date,
                year=parse_year(date),
                raw={"creator": creator, "source": "sru"},
            )
        )
        if len(records) >= limit:
            break
    return records


def build_bnb_query(title: str, creator: str | None, limit: int) -> str:
    """Return the BNB SPARQL query for works whose title contains *title*."""
    t = escape_sparql_string(title)
    c = escape_sparql_string(creator) if creator else None
    creator_filter = (
        f'  FILTER(CONTAINS(LCASE(COALESCE(STR(?creatorName), "")), LCASE("{c}")))\n'
        if c
        else ""
    )
    return (
        "PREFIX dct: <http://purl.org/dc/terms/>\n"
        "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>\n"
        "PREFIX foaf: <http://xmlns.com/foaf/0.1/>\n"
        "PREFIX owl: <http://www.w3.org/2002/07/owl#>\n"
        "SELECT ?work ?title ?creatorName ?same ?ident WHERE {\n"
        "  ?work dct:title ?title .\n"
        f'  FILTER(CONTAINS(LCASE(STR(?title)), LCASE("{t}")))\n'
        "  OPTIONAL {\n"
        "    ?work dct:creator ?creator .\n"
        "    OPTIONAL { ?creator foaf:name ?creatorName }\n"
        "    OPTIONAL { ?creator rdfs:label ?creatorName }\n"
        "  }\n"
        "  OPTIONAL { ?work owl:sameAs ?same }\n"
        "  OPTIONAL { ?work rdfs:seeAlso ?same }\n"
        "  OPTIONAL { ?work dct:identifier ?ident }\n"
        f"{creator_filter}"
        "}\n"
        f"LIMIT {max(5, limit * 3)}\n"
    )


def _binding_value(binding: Mapping[str, Any], name: str) -> str | None:
    cell = binding.get(name)
    if isinstance(cell, Mapping):
        value = cell.get("value")
        return str(value) if value else None
    return None


def parse_bnb_bindings(document: Any, *, limit: int) -> list[SearchRecord]:
    """Return at most *limit* records of a BNB SPARQL result with a BL ARK.

    The ARK is read from ``?same``, ``?ident`` or ``?work``. The title and
    creator are the binding's own: a binding without a title gets an empty
    one, never the query's.
    """
    results = document.get("results") if isinstance(document, Mapping) else None
    bindings = results.get("bindings") if isinstance(results, Mapping) else None
    records: list[SearchRecord] = []
    for binding in bindings if isinstance(bindings, list) else []:
        if not isinstance(binding, Mapping):
            continue
        ark = None
        for name in ("same", "ident", "work"):
            match = _BNB_ARK.search(_binding_value(binding, name) or "")
            if match is not None:
                ark = match.group(1)
                break
        if ark is None:
            continue
        creator = _binding_value(binding, "creatorName")
        records.append(
            SearchRecord(
                provider=KEY,
                source_id=ark,
                title=_binding_value(binding, "title") or "",
                creators=(creator,) if creator else (),
                raw={"creator": creator, "source": "bnb_sparql"},
            )
        )
        if len(records) >= limit:
            break
    return records


def _search_failure(error: RequestFailed | BreakerOpen) -> ProviderError:
    if isinstance(error, RequestFailed):
        return ProviderBlocked(KEY, error.cause.value, detail=str(error))
    return provider_error(KEY, error)


@dataclass(frozen=True)
class BritishLibrary:
    """The British Library adapter; endpoint URLs are fields for tests.

    ``source_id`` is the BL ARK's name part (``vdc_...``, with a viewer
    suffix such as ``.0x000002`` where the record carries one); the
    manifest URL uses the part before the first dot.
    """

    hosts: ClassVar[frozenset[str]] = frozenset({"bl.uk"})
    sru_url: str = SRU_URL
    bnb_sparql_url: str = BNB_SPARQL_URL
    manifest_url: str = MANIFEST_URL
    viewer_url: str = VIEWER_URL

    @property
    def key(self) -> str:
        """The provider key."""
        return KEY

    @property
    def capabilities(self) -> Capabilities:
        """What the adapter offers."""
        return Capabilities(iiif=True)

    def _search_sru(
        self, query: SearchQuery, network: Network, limit: int
    ) -> list[SearchRecord]:
        q_title = escape_sru_literal(query.title)
        cql = f'title all "{q_title}"'
        if query.creator:
            cql += f' and creator all "{escape_sru_literal(query.creator)}"'
        params = {
            "version": "1.2",
            "operation": "searchRetrieve",
            "query": cql,
            "maximumRecords": str(limit),
            "recordSchema": "dc",
        }
        response = network.request(
            KEY,
            "GET",
            self.sru_url,
            params=params,
            headers={"Accept": "application/xml,text/xml"},
        )
        try:
            return parse_sru_records(response.content, limit=limit)
        except ET.ParseError as exc:
            raise InvalidResponse(KEY, self.sru_url, f"not XML: {exc}") from exc

    def _search_bnb(
        self, query: SearchQuery, network: Network, limit: int
    ) -> list[SearchRecord]:
        response = network.request(
            KEY,
            "GET",
            self.bnb_sparql_url,
            params={"query": build_bnb_query(query.title, query.creator, limit)},
            headers={"Accept": "application/sparql-results+json"},
        )
        try:
            document = response.json()
        except ValueError as exc:
            raise InvalidResponse(KEY, self.bnb_sparql_url, "not JSON") from exc
        return parse_bnb_bindings(document, limit=limit)

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        """Return SRU records with an ARK, else the BNB SPARQL records.

        Raises:
            ProviderBlocked: The breaker is open, or SRU failed and the
                fallback found nothing or failed; the cause is SRU's.
            InvalidResponse: An endpoint answered with an unreadable body.
        """
        if limit < 1:
            return []
        primary: ProviderError | None = None
        try:
            records = self._search_sru(query, network, limit)
        except RequestFailed as exc:
            logger.warning("%s: SRU search failed: %s", KEY, exc)
            primary = _search_failure(exc)
        except InvalidResponse as exc:
            logger.warning("%s: %s", KEY, exc)
            primary = exc
        except BreakerOpen as exc:
            raise _search_failure(exc) from exc
        else:
            if records:
                return records
        logger.info("%s: trying the BNB SPARQL fallback for %r", KEY, query.title)
        try:
            found = self._search_bnb(query, network, limit)
        except RequestFailed as exc:
            if isinstance(primary, ProviderBlocked):
                raise ProviderBlocked(
                    KEY, primary.cause, detail=f"{primary.detail}; BNB SPARQL: {exc}"
                ) from exc
            raise _search_failure(exc) from exc
        except BreakerOpen as exc:
            raise _search_failure(exc) from exc
        if not found and primary is not None:
            raise primary
        return found

    def _discover_manifest(
        self, network: Network, identifier: str, slot: SlotToken | None
    ) -> tuple[Mapping[str, Any], str] | None:
        """Return the manifest the viewer page links to, or None.

        Raises:
            ProviderBlocked: The breaker is open, the quota is spent or the
                slot was lost.
        """
        viewer = self.viewer_url.format(identifier=identifier)
        logger.info("%s: looking up the manifest in %s", KEY, viewer)
        try:
            html = network.request(KEY, "GET", viewer, slot=slot).text
        except RequestFailed as exc:
            logger.warning("%s: viewer page failed: %s", KEY, exc)
            return None
        except (BreakerOpen, QuotaSpent, SlotLost) as exc:
            raise provider_error(KEY, exc, identifier) from exc
        match = _VIEWER_MANIFEST.search(html)
        if match is None:
            return None
        url = match.group(0)
        try:
            return fetch_json(network, KEY, url, slot=slot, item=identifier), url
        except ProviderBlocked as exc:
            if exc.cause in _HALTS:
                raise
            logger.warning("%s: discovered manifest failed: %s", KEY, exc)
        except (ItemNotFound, InvalidResponse) as exc:
            logger.warning("%s: discovered manifest failed: %s", KEY, exc)
        return None

    def _manifest(
        self, network: Network, record_or_id: SearchRecord | str, slot: SlotToken | None
    ) -> tuple[str, Mapping[str, Any], str]:
        """Return the identifier, the manifest and its URL.

        Raises:
            ItemNotFound: No identifier, or the manifest is not there.
            ProviderBlocked: The manifest request failed and the viewer
                lookup found nothing; or the download must stop.
            InvalidResponse: The manifest is not JSON and the viewer lookup
                found nothing.
        """
        identifier = source_id_of(record_or_id).strip()
        if not identifier:
            raise ItemNotFound(KEY, identifier)
        url = self.manifest_url.format(identifier=identifier.split(".")[0])
        try:
            return identifier, fetch_json(network, KEY, url, slot=slot), url
        except ProviderBlocked as exc:
            if exc.cause in _HALTS:
                raise
            failure: ProviderError = exc
        except (ItemNotFound, InvalidResponse) as exc:
            failure = exc
        logger.warning("%s: manifest failed: %s", KEY, failure)
        found = self._discover_manifest(network, identifier, slot)
        if found is None:
            raise failure
        manifest, url = found
        return identifier, manifest, url

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        """Stage the manifest's rendering and page images.

        Raises:
            ItemNotFound, ProviderBlocked, InvalidResponse: As
                :meth:`_manifest`.
            OSError: A file could not be written.
        """
        identifier, manifest, url = self._manifest(network, record_or_id, request.slot)
        return download_manifest(
            network,
            KEY,
            manifest,
            source_id=identifier,
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
        """Return the manifest's first *first_n* canvases at *max_px*.

        Raises:
            ValueError: *first_n* or *max_px* is below 1.
            ItemNotFound, ProviderBlocked, InvalidResponse: As
                :meth:`_manifest`.
        """
        if first_n < 1 or max_px < 1:
            raise ValueError("first_n and max_px must be at least 1")
        _identifier, manifest, _url = self._manifest(network, record_or_id, None)
        return manifest_page_refs(manifest, first_n=first_n, max_px=max_px)

    def item_id_from_url(self, url: str) -> str | None:
        """Return the ARK name of a viewer or manifest URL.

        Viewer URLs read ``access.bl.uk/item/viewer/ark:/81055/vdc_...``; the
        viewer suffix is kept.
        """
        if not on_hosts(url, self.hosts):
            return None
        return path_match(url, _SRU_ARK)

    def item_manifest_url(self, item_id: str) -> str:
        """Return the item's manifest URL (the ARK name before its first dot)."""
        return self.manifest_url.format(identifier=item_id.split(".")[0])


def build(settings: Settings) -> BritishLibrary:
    """Return the British Library adapter; it reads nothing from *settings*."""
    return BritishLibrary()
