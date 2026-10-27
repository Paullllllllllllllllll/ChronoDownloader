"""MDZ (Münchener DigitalisierungsZentrum): JSON search, IIIF downloads.

Ported from ChronoDownloader v1.30.3 ``api/providers/mdz.py`` on 19.10.2026.
Search asks the website's JSON API for digitized items and, when it returns
none, reads the result links of the HTML search page; downloads fetch the
IIIF Presentation v2 manifest, else the v3 one. Changed: highlight tags are
stripped and HTML entities decoded in titles and creators at parse time
(v1.30.3 left ``<em>`` in creators and ``&apos;`` and ``&amp;`` in both);
a failed request raises instead of yielding no results.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final

from bs4 import BeautifulSoup

from ._query import parse_year
from ._urls import on_hosts, path_match
from .base import (
    Capabilities,
    DownloadRequest,
    DownloadResult,
    InvalidResponse,
    ItemNotFound,
    PageRef,
    Provider,
    SearchQuery,
    SearchRecord,
    source_id_of,
)
from .iiif_download import download_manifest, fetch_json, manifest_page_refs

if TYPE_CHECKING:
    import requests

    from chronodownloader.coord import SlotToken
    from chronodownloader.net import Network
    from chronodownloader.settings import Settings

__all__ = ["Mdz", "build"]

logger = logging.getLogger(__name__)

KEY: Final = "mdz"
SEARCH_URL = "https://www.digitale-sammlungen.de/api/search"
HTML_SEARCH_URL = "https://www.digitale-sammlungen.de/en/search"
MANIFEST_V2_URL = (
    "https://api.digitale-sammlungen.de/iiif/presentation/v2/{object_id}/manifest"
)
MANIFEST_V3_URL = (
    "https://api.digitale-sammlungen.de/iiif/presentation/v3/{object_id}/manifest"
)
ITEM_URL = "https://www.digitale-sammlungen.de/view/{object_id}"

_TAG = re.compile(r"<[^>]+>")
# The language segment is optional as a whole, so an unprefixed
# "/view/bsb..." href matches as well as "/en/view/bsb...".
_VIEW_HREF = re.compile(r"/(?:(?:en|de)/)?view/([^/?#]+)")
_BSB_ID = re.compile(r"/(bsb\d+)(?:[/.?#]|$)", re.IGNORECASE)


def clean_text(value: Any) -> str:
    """Return *value* as text without highlight tags and HTML entities."""
    return html.unescape(_TAG.sub("", str(value))).strip()


def _creators(authors: Any) -> tuple[str, ...]:
    values = authors if isinstance(authors, list) else [authors] if authors else []
    cleaned = (clean_text(author) for author in values if author is not None)
    return tuple(name for name in cleaned if name)


def _api_record(doc: Any) -> SearchRecord | None:
    """Return the record of one API document, or None for an unusable one."""
    if not isinstance(doc, Mapping) or doc.get("iiifAvailable") is False:
        return None
    object_id = doc.get("id")
    if not object_id:
        return None
    title = doc.get("title") or ""
    if isinstance(title, list):
        title = title[0] if title else ""
    date = str(doc["publicationDate"]) if doc.get("publicationDate") else None
    return SearchRecord(
        provider=KEY,
        source_id=str(object_id),
        title=clean_text(title),
        creators=_creators(doc.get("authors")),
        date=date,
        year=parse_year(date),
        item_url=ITEM_URL.format(object_id=object_id),
    )


def parse_api_search(response: requests.Response) -> list[SearchRecord]:
    """Return the records of a JSON API answer; none when it is not JSON."""
    try:
        data = response.json()
    except ValueError:
        return []
    docs = data.get("docs") if isinstance(data, Mapping) else None
    if not isinstance(docs, list):
        return []
    records = (_api_record(doc) for doc in docs)
    return [record for record in records if record is not None]


def parse_html_search(text: str, limit: int) -> list[SearchRecord]:
    """Return the items the HTML search page links to, at most *limit*.

    The page names no author, and the query's creator is never put in its
    place: echoing it back would score a perfect match against itself.
    """
    records: list[SearchRecord] = []
    seen: set[str] = set()
    for anchor in BeautifulSoup(text, "html.parser").find_all("a", href=True):
        match = _VIEW_HREF.search(str(anchor["href"]))
        if not match or match.group(1) in seen:
            continue
        object_id = match.group(1)
        seen.add(object_id)
        records.append(
            SearchRecord(
                provider=KEY,
                source_id=object_id,
                title=anchor.get_text(strip=True),
                item_url=ITEM_URL.format(object_id=object_id),
            )
        )
        if len(records) >= limit:
            break
    return records


class Mdz:
    """The MDZ adapter; ``source_id`` is the BSB object id (``bsb...``).

    ``search`` lets :class:`~chronodownloader.net.RequestFailed`,
    :class:`~chronodownloader.net.BreakerOpen` and
    :class:`~chronodownloader.net.QuotaSpent` propagate; ``download`` and
    ``pages`` raise provider errors.
    """

    key: Final = KEY
    capabilities: Final = Capabilities(iiif=True)
    hosts: ClassVar[frozenset[str]] = frozenset({"digitale-sammlungen.de"})

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        """Return at most *limit* digitized items, from the API or the HTML page."""
        terms = f"{query.title} {query.creator}" if query.creator else query.title
        logger.info("Searching MDZ for: %s", query.title)
        response = network.request(
            KEY,
            "GET",
            SEARCH_URL,
            params={
                "query": terms,
                "handler": "simple-metadata",
                "pageSize": limit,
                "ocrContext": 1,
            },
        )
        records = parse_api_search(response)
        if records:
            return records[:limit]
        page = network.request(KEY, "GET", HTML_SEARCH_URL, params={"search": terms})
        return parse_html_search(page.text, limit)

    def _manifest(
        self, network: Network, object_id: str, slot: SlotToken | None
    ) -> tuple[str, Mapping[str, Any]]:
        """Return the URL and document of the v2 manifest, else the v3 one.

        Raises:
            ItemNotFound: Neither manifest exists.
            ProviderBlocked: A request failed otherwise.
            InvalidResponse: The v3 answer is not a JSON object.
        """
        if not object_id:
            raise ItemNotFound(KEY, object_id)
        url = MANIFEST_V2_URL.format(object_id=object_id)
        try:
            return url, fetch_json(network, KEY, url, slot=slot, item=object_id)
        except (ItemNotFound, InvalidResponse) as exc:
            logger.info("MDZ: no v2 manifest for %s (%s); trying v3", object_id, exc)
        url = MANIFEST_V3_URL.format(object_id=object_id)
        return url, fetch_json(network, KEY, url, slot=slot, item=object_id)

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        """Stage the item's manifest, rendering and page images."""
        object_id = source_id_of(record_or_id)
        url, manifest = self._manifest(network, object_id, request.slot)
        return download_manifest(
            network,
            KEY,
            manifest,
            source_id=object_id,
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
        """Return the first *first_n* canvases of the item at *max_px*."""
        _url, manifest = self._manifest(network, source_id_of(record_or_id), None)
        return manifest_page_refs(manifest, first_n=first_n, max_px=max_px)

    def item_id_from_url(self, url: str) -> str | None:
        """Return the BSB id of a viewer, details or manifest URL.

        These read ``/view/bsb00027043``, ``/de/details/...`` and
        ``.../v2/bsb.../manifest``.
        """
        if not on_hosts(url, self.hosts):
            return None
        return path_match(url, _BSB_ID)

    def item_manifest_url(self, item_id: str) -> str:
        """Return the item's Presentation v2 manifest URL."""
        return MANIFEST_V2_URL.format(object_id=item_id)


def build(settings: Settings) -> Provider:
    """Return the MDZ adapter; it reads no settings."""
    return Mdz()
