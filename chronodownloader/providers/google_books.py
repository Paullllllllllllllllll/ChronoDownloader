"""Google Books: volume search, direct PDF or EPUB files and page images.

Ported from ChronoDownloader v1.30.3 ``api/providers/google_books.py`` on
19.10.2026. The four query variants times the viewability filters, the
``free_only``, ``prefer``, ``allow_drm`` and ``max_files`` options, the page
image URLs and the placeholder detection carry over. Changed: a failed
search request no longer reads as "no results": a variant that fails moves
on to the next one, and when no variant finds a volume the last failure is
raised (v1.30.3 reported a 503 storm as "no items found"). A placeholder
page is recognized before it is kept, so it is never charged to the size
budget and stays missing in the result. The cover image fallback, which
never counted as content, is gone.

``max_files`` bounds the direct download links tried for the volume's one
rendering; the page limit is the run's ``max_pages`` (the provider default
is 50). ``source_id`` is the volume id.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Literal

from chronodownloader.coord import SlotLost
from chronodownloader.iiif import Rendering
from chronodownloader.net import BreakerOpen, Network, QuotaSpent, RequestFailed
from chronodownloader.settings import GoogleBooksSettings, Settings

from ._file_download import DownloadRun, get_json, wants_images
from ._query import parse_year
from ._urls import on_hosts, path_match, query_value
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
    UnsupportedCapability,
    page_item,
    provider_error,
    source_id_of,
)
from .staging import stage_download

__all__ = [
    "API_URL",
    "CONTENT_URL",
    "GoogleBooks",
    "build",
    "is_placeholder_image",
    "query_variants",
]

logger = logging.getLogger(__name__)

KEY = "google_books"
API_URL = "https://www.googleapis.com/books/v1/volumes"
CONTENT_URL = "https://books.google.com/books/content"
METADATA_NAME = "volume"
"""The staged volume response (``metadata/volume.json``)."""

DEFAULT_PAGES = 50
"""Pages tried when neither a page limit nor the volume's page count is known."""
EXTENT_UNKNOWN = "extent_unknown"
"""The cause of the page after an unbounded probe: the extent is unknown."""
MAX_CONSECUTIVE_PLACEHOLDERS = 3
MAX_CONSECUTIVE_FAILURES = 5

PLACEHOLDER_SIGNATURES = frozenset(
    {
        # The standard "page not available" placeholder (gray image with text).
        (9103, "a64fa89d7ebc97075c1d363fc5fea71f"),
        # The same size with any content.
        (9103, None),
    }
)
"""Known placeholder images, by size in bytes and MD5 (None: any content)."""
MIN_VALID_IMAGE_SIZE = 15_000
"""Real scanned pages are usually larger; smaller files are checked."""
MAX_PLACEHOLDER_SIZE = 10_000
"""A page image below this size is taken for a placeholder."""

_FULL_VIEW = ("all_pages", "all_pages_public_domain", "full_public_domain", "full")
_VOLUME_PATH = re.compile(r"/(?:books/v1/volumes|books/edition/[^/]+)/([\w-]+)")
_VOLUME_ID = re.compile(r"[\w-]{6,}")

type _PageOutcome = Literal["page", "placeholder", "failed"]


def is_placeholder_image(path: Path) -> bool:
    """Return True when the image at *path* is a Google Books placeholder.

    Google Books answers an unavailable page with a small placeholder image
    instead of a 404: a known size and hash, or any image under 10 kB.
    """
    try:
        size = path.stat().st_size
        if size >= MIN_VALID_IMAGE_SIZE:
            return False
        digest = hashlib.md5(path.read_bytes()).hexdigest().lower()
    except OSError as exc:
        logger.debug("%s: cannot check %s for a placeholder: %s", KEY, path, exc)
        return False
    for signature_size, signature_hash in PLACEHOLDER_SIGNATURES:
        if size == signature_size and signature_hash in (None, digest):
            return True
    return size < MAX_PLACEHOLDER_SIZE


def query_variants(title: str, creator: str | None) -> list[str]:
    """Return the search queries, strictest first.

    Quoted ``intitle``/``inauthor`` clauses (embedded quotes removed), the
    same unquoted, plain title and creator, and the title stripped of
    punctuation. Clauses are joined with a space: a literal ``+`` would be
    sent as ``%2B``, a plus sign rather than the separator.
    """
    safe_title = title.replace('"', " ")
    strict = f'intitle:"{safe_title}"'
    loose = f"intitle:{title}"
    if creator:
        safe_creator = creator.replace('"', " ")
        strict += f' inauthor:"{safe_creator}"'
        loose += f" inauthor:{creator}"
    plain = f"{title} {creator}" if creator else title
    sanitized = re.sub(r"[^\w\s]", " ", title).strip()
    return [query for query in (strict, loose, plain, sanitized) if query.strip()]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _download_link(access: Mapping[str, Any], prefer: str) -> str | None:
    """Return the volume's direct download link, the preferred format first."""
    for fmt in (prefer, "pdf", "epub"):
        link = _mapping(access.get(fmt)).get("downloadLink")
        if link:
            return str(link)
    return None


def _full_view(viewability: str) -> bool:
    return any(token in viewability for token in _FULL_VIEW)


@dataclass(frozen=True)
class GoogleBooks:
    """The Google Books adapter.

    *api_key* is sent as the ``key`` parameter when set; *options* holds
    ``free_only``, ``prefer``, ``allow_drm`` and ``max_files``. The endpoint
    fields exist for tests.
    """

    hosts: ClassVar[frozenset[str]] = frozenset(
        {"www.googleapis.com", "books.google.com"}
    )
    options: GoogleBooksSettings = field(default_factory=GoogleBooksSettings)
    api_key: str | None = field(default=None, repr=False)
    api_url: str = API_URL
    content_url: str = CONTENT_URL

    @property
    def key(self) -> str:
        return KEY

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(api_key=KEY)

    @property
    def prefer(self) -> str:
        return str(self.options.prefer or "pdf").lower()

    def _params(self, query: str, flt: str | None, limit: int) -> dict[str, str]:
        params = {
            "q": query,
            "maxResults": str(limit),
            "printType": "books",
            "orderBy": "relevance",
            "projection": "full",
            "country": "US",
        }
        if self.api_key:
            params["key"] = self.api_key
        if flt:
            params["filter"] = flt
        # download=epub restricts the hits to downloadable EPUBs; asked only
        # when EPUB is preferred, so PDF-only free books are not excluded.
        if self.prefer == "epub":
            params["download"] = "epub"
        return params

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        """Try the query variants under each filter until one finds volumes.

        The filters are ``free-ebooks``, ``ebooks`` and none under
        ``free_only``, else ``ebooks`` and none.

        Raises:
            ProviderBlocked: The breaker opened, or every variant found
                nothing and one of them failed (the last failure's cause).
            InvalidResponse: An answer is not a JSON object.
        """
        filters: list[str | None] = (
            ["free-ebooks", "ebooks", None]
            if self.options.free_only
            else ["ebooks", None]
        )
        logger.info("Searching Google Books for: %s", query.title)
        failure: RequestFailed | None = None
        for text in query_variants(query.title, query.creator):
            for flt in filters:
                try:
                    response = network.request(
                        KEY, "GET", self.api_url, params=self._params(text, flt, limit)
                    )
                except RequestFailed as exc:
                    logger.warning("%s: search variant failed: %s", KEY, exc)
                    failure = exc
                    continue
                except (BreakerOpen, QuotaSpent) as exc:
                    raise provider_error(KEY, exc) from exc
                try:
                    data = response.json()
                except ValueError as exc:
                    raise InvalidResponse(KEY, self.api_url, "not JSON") from exc
                items = data.get("items") if isinstance(data, Mapping) else None
                if isinstance(items, list) and items:
                    return self._records(items)[:limit]
        if failure is not None:
            raise provider_error(KEY, failure)
        return []

    def _records(self, items: list[Any]) -> list[SearchRecord]:
        records: list[SearchRecord] = []
        for item in items:
            if not isinstance(item, Mapping):
                logger.warning("%s: skipping malformed record %r", KEY, item)
                continue
            record = self._record(item)
            if record is not None:
                records.append(record)
        return records

    def _record(self, item: Mapping[str, Any]) -> SearchRecord | None:
        """Return the record of one volume; None when ``free_only`` excludes it.

        Under ``free_only`` a volume must offer a direct download, be public
        domain or be fully viewable.
        """
        volume = _mapping(item.get("volumeInfo"))
        access = _mapping(item.get("accessInfo"))
        volume_id = item.get("id")
        if not isinstance(volume_id, str) or not volume_id:
            logger.warning("%s: skipping volume without id", KEY)
            return None
        link = _download_link(access, self.prefer)
        public_domain = bool(access.get("publicDomain"))
        viewability = str(
            access.get("viewability") or volume.get("viewability") or ""
        ).lower()
        if self.options.free_only and not (
            link or public_domain or _full_view(viewability)
        ):
            return None
        creators = [str(author) for author in (volume.get("authors") or [])]
        date = volume.get("publishedDate")
        date_text = str(date) if date else None
        item_url = f"https://books.google.com/books?id={volume_id}"
        raw = {
            "title": volume.get("title") or "",
            "creators": creators,
            "date": date,
            "id": volume_id,
            "item_url": item_url,
            "accessInfo": dict(access),
            "viewability": viewability,
            "publicDomain": public_domain,
            "has_download": bool(link),
        }
        return SearchRecord(
            provider=KEY,
            source_id=volume_id,
            title=str(raw["title"]),
            creators=tuple(creators),
            date=date_text,
            year=parse_year(date_text),
            item_url=item_url,
            raw=MappingProxyType(raw),
        )

    def _links(self, access: Mapping[str, Any]) -> list[Rendering]:
        """Return the direct download links, the preferred format first.

        A format counts when it is available and has a download link; under
        ``allow_drm`` its ACS token link counts otherwise. At most
        ``max_files`` distinct links are returned.
        """
        other = "epub" if self.prefer == "pdf" else "pdf"
        links: list[Rendering] = []
        for fmt in (self.prefer, other):
            entry = _mapping(access.get(fmt))
            if entry.get("isAvailable") and entry.get("downloadLink"):
                url = str(entry["downloadLink"])
            elif self.options.allow_drm and entry.get("acsTokenLink"):
                url = str(entry["acsTokenLink"])
            else:
                continue
            if all(link.url != url for link in links):
                links.append(Rendering(url, fmt, None))
        return links[: self.options.max_files]

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        """Stage the volume's rendering and/or page images.

        Page images are tried only for public-domain or fully viewable
        volumes; three placeholders or five failed pages in a row end them.

        Raises:
            ItemNotFound: The volume id is empty or unknown.
            ProviderBlocked, InvalidResponse: The volume could not be had.
            OSError: A file could not be written.
        """
        volume_id = source_id_of(record_or_id).strip()
        if not volume_id:
            raise ItemNotFound(KEY, volume_id)
        url = f"{self.api_url}/{urllib.parse.quote(volume_id, safe='')}"
        params = {"key": self.api_key} if self.api_key else None
        volume = get_json(
            network, KEY, url, params=params, slot=request.slot, item=volume_id
        )
        staging_dir.mkdir(parents=True, exist_ok=True)
        run = DownloadRun(network, KEY, staging_dir, request)
        run.stage_metadata(METADATA_NAME, volume, source_url=url)
        access = _mapping(volume.get("accessInfo"))
        viewability = str(access.get("viewability") or "").lower()
        has_pages = bool(access.get("publicDomain")) or (
            "all_pages" in viewability or viewability == "full"
        )
        offered = self._links(access) if request.format != "images" else []
        parts: list[Completeness] = []
        rendering_failed = False
        if offered:
            pdf = run.stage_rendering(offered)
            parts.append(pdf)
            rendering_failed = not pdf.complete and run.stopped is None
        if wants_images(
            request.format,
            offered=bool(offered),
            rendering_failed=rendering_failed,
            has_pages=has_pages,
        ):
            if has_pages:
                page_count = _mapping(volume.get("volumeInfo")).get("pageCount")
                parts.append(self._stage_pages(run, volume_id, page_count))
            else:
                parts.append(Completeness("images", (), ()))
        return run.result(volume_id, parts)

    def _page_urls(self, volume_id: str, page: int) -> list[str]:
        """Return the page's image URLs: printed page ``PA``, then ``PT``."""
        quoted = urllib.parse.quote(volume_id)
        return [
            f"{self.content_url}?id={quoted}&pg={kind}{page}&img=1&zoom=3"
            for kind in ("PA", "PT")
        ]

    def _stage_pages(
        self, run: DownloadRun, volume_id: str, page_count: Any
    ) -> Completeness:
        """Stage page images through the content endpoint.

        The pages expected are the first ones within the page limit and the
        volume's page count. When neither is known, the first
        :data:`DEFAULT_PAGES` pages are probed and the page after them is
        expected too, missing with cause :data:`EXTENT_UNKNOWN`: the
        volume's extent is unknown, so the probe never completes it.
        """
        limit = run.request.page_limit
        known = page_count if isinstance(page_count, int) and page_count > 0 else 0
        bounds = [value for value in (limit, known) if value]
        total = min(bounds) if bounds else DEFAULT_PAGES
        expected = tuple(page_item(page) for page in range(1, total + 1))
        causes: dict[str, str] = {}
        if not bounds:
            beyond = page_item(total + 1)
            expected = (*expected, beyond)
            causes[beyond] = EXTENT_UNKNOWN
        present = set(expected) & run.request.present
        placeholders = failures = 0
        for page in range(1, total + 1):
            item = page_item(page)
            if item in present:
                continue
            if failures >= MAX_CONSECUTIVE_FAILURES:
                logger.info("%s: %d pages failed in a row; stopping", KEY, failures)
                break
            if placeholders >= MAX_CONSECUTIVE_PLACEHOLDERS:
                logger.info(
                    "%s: %d placeholder pages in a row for %s; stopping",
                    KEY,
                    placeholders,
                    volume_id,
                )
                break
            if run.halt is not None or not run.admits("image"):
                break
            try:
                outcome = self._stage_page(run, volume_id, page)
            except (BreakerOpen, QuotaSpent, SlotLost) as exc:
                run.stop(exc)
                break
            if outcome == "page":
                present.add(item)
                placeholders = failures = 0
            elif outcome == "placeholder":
                placeholders += 1
            else:
                failures += 1
        return Completeness(
            "images",
            expected,
            tuple(item for item in expected if item in present),
            page_limit=limit,
            available=known or total,
            causes={
                item: cause for item, cause in causes.items() if item not in present
            },
        )

    def _stage_page(self, run: DownloadRun, volume_id: str, page: int) -> _PageOutcome:
        """Stage one page from the first URL that answers.

        A placeholder is deleted and not tried under the other URL.

        Raises:
            BreakerOpen, QuotaSpent, SlotLost, OSError: The download must stop.
        """
        item = page_item(page)
        for url in self._page_urls(volume_id, page):
            try:
                staged = stage_download(
                    run.network,
                    KEY,
                    url,
                    run.staging_dir,
                    item,
                    slot=run.slot,
                    kind="image",
                )
            except RequestFailed as exc:
                logger.info("%s: %s failed: %s", KEY, item, exc)
                continue
            path = run.staging_dir / staged.name
            if is_placeholder_image(path):
                logger.debug("%s: %s is a placeholder; removed", KEY, item)
                path.unlink(missing_ok=True)
                return "placeholder"
            run.keep(staged)
            return "page"
        return "failed"

    def pages(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        first_n: int,
        max_px: int,
    ) -> list[PageRef]:
        """Google Books has no page images at a requested size.

        Raises:
            UnsupportedCapability: Always.
        """
        raise UnsupportedCapability(KEY, "pages")

    def item_id_from_url(self, url: str) -> str | None:
        """Return the volume id of a books page (``books?id=<id>``) or of an
        API volume URL (``/books/v1/volumes/<id>``)."""
        if not on_hosts(url, self.hosts):
            return None
        found = path_match(url, _VOLUME_PATH) or query_value(url, "id")
        return found if found and _VOLUME_ID.fullmatch(found) else None


def build(settings: Settings) -> GoogleBooks:
    """Return the adapter with the Google Books block and the key from the
    environment variable that ``api_keys`` names."""
    block = settings.providers.get(KEY)
    options = block if isinstance(block, GoogleBooksSettings) else GoogleBooksSettings()
    api_key = os.environ.get(settings.api_key_env(KEY)) or None
    return GoogleBooks(options=options, api_key=api_key)
