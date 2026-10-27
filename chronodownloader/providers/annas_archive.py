"""Anna's Archive: HTML search, the member fast-download API and public links.

Ported from ChronoDownloader v1.30.3 ``api/providers/annas_archive.py`` and
the Anna's Archive login-page checks of ``api/core/download.py`` (lines
213-219, 323-328, 369-401) on 19.10.2026. Changed: the mirror comes from
``providers.annas_archive.base_url`` (empty: the adapter raises
:class:`MirrorNotConfigured`); the fast-download API call is reserved against
the daily quota through the coordinator (``quota=True``), and a spent quota,
local or reported by the server, stops the download with ``quota_spent``
instead of raising ``QuotaDeferredException``; the thread-local "fast API
used" flag became ``DownloadResult.quota_charged``; the best title among a
row's title snippets is no longer chosen by fuzzy score against the query
(matching belongs to the policy layer) but is the row's first snippet, the
anchor text of its ``/md5/`` link, with every snippet kept in
``raw["title_candidates"]``; a search page that is not an Anna's Archive page
(a parked or moved mirror) raises :class:`InvalidResponse` instead of
returning no candidates. The member key is sent only as the API's ``key``
parameter and never logged, staged or shown in ``repr``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, ClassVar
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup, Tag

from chronodownloader.coord import SlotLost
from chronodownloader.net import (
    BreakerOpen,
    Cause,
    Network,
    QuotaSpent,
    RequestFailed,
)
from chronodownloader.net.atomic import replace_with_retry
from chronodownloader.net.credentials import redact_url
from chronodownloader.net.payload import (
    extension_for_content_type,
    magic_mismatch,
)
from chronodownloader.settings import AnnasArchiveSettings, Settings

from ._query import parse_year
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
    ProviderBlocked,
    ProviderError,
    SearchQuery,
    SearchRecord,
    StagedFile,
    StopReason,
    UnsupportedCapability,
    provider_error,
    source_id_of,
)
from .staging import DOCUMENT_EXTENSIONS, stage_metadata

__all__ = [
    "KEY",
    "LOGIN_MARKERS",
    "AnnasArchive",
    "FastDownload",
    "Md5Page",
    "MirrorNotConfigured",
    "build",
    "is_annas_page",
    "login_page_reason",
    "parse_md5_page",
    "parse_search_page",
    "read_fast_download",
]

logger = logging.getLogger(__name__)

KEY = "annas_archive"

SEARCH_PATH = "/search"
MD5_PATH = "/md5/"
FAST_DOWNLOAD_PATH = "/dyn/api/fast_download.json"

MAX_LINK_ATTEMPTS = 5
"""Public download links tried per item, in priority order."""

FILE_EXTENSIONS = (".pdf", ".epub", ".djvu", ".mobi", ".azw", ".azw3", ".fb2")
"""URL suffixes that mark a direct file link on an MD5 page."""

_SKIPPED_LINKS = (
    "account",
    "login",
    "register",
    "donate",
    "torrents",
    "datasets",
    "blog",
    "about",
    "faq",
)
_MIRROR_HOSTS = ("libgen", "library.lol", "z-lib", "zlibrary", "ipfs")
_SCRIPT_FILE_URL = re.compile(
    r'https?://[^"\s<>]+(?:\.pdf|\.epub|\.djvu|\.mobi)[^"\s<>]*'
)

LOGIN_MARKERS = (
    "<title>log in / register",
    "<title>login",
    "member login",
    "please log in",
    "__darkreader__",
)
"""Text in the first characters of an HTML body that marks Anna's Archive's
login or error page."""

LOGIN_CHECK_CHARS = 2048
LOGIN_PAGE_BYTES = (170_000, 185_000)
"""Exclusive size bounds of the HTML login or error page (about 180 KB)."""

_HEAD_BYTES = 4096
_BRAND = re.compile(r"anna(?:'|’|&#39;|&#x27;)s\s+archive", re.IGNORECASE)
_HEX = frozenset("0123456789abcdefABCDEF")
_REFUSALS = frozenset({401, 403})
"""Fast-download API statuses that refuse the key before serving anything
("Not a member"): no retry, the quota unit given back, the body's error kept."""
_SUFFIX_ALIASES = {".jpeg": ".jpg", ".tiff": ".tif"}


class MirrorNotConfigured(ProviderError):
    """No Anna's Archive mirror is configured, so the adapter is unavailable."""

    def __init__(self) -> None:
        super().__init__(
            KEY,
            "unavailable: no mirror configured; set providers.annas_archive.base_url",
        )


def _dedup_key(text: str) -> str:
    """Return *text* lowercased, without accents and punctuation, for dedup."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^\w]+|_", " ", plain.casefold()).split())


def _clean_title_candidate(text: str) -> str:
    """Normalize spacing and trim overly long or concatenated titles."""
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return ""
    years = list(re.finditer(r"\b(19|20)\d{2}\b", cleaned))
    if len(years) >= 2 and years[1].start() > 20:
        cleaned = cleaned[: years[1].start()].strip()
    if cleaned.count("(") > 1 and cleaned.endswith(")"):
        last_paren = cleaned.rfind("(")
        if last_paren > 20:
            potential = cleaned[:last_paren].strip()
            if len(potential) > 15:
                cleaned = potential
    cleaned = cleaned.rstrip(" -:;|/")
    if len(cleaned) > 100:
        truncated = cleaned[:100].strip()
        last_space = truncated.rfind(" ")
        if last_space > 40:
            truncated = truncated[:last_space].strip()
        cleaned = truncated
    return cleaned


def _collect_title_candidates(texts: Iterable[str]) -> list[str]:
    """Return unique, cleaned title candidates from raw snippets, in order."""
    candidates: list[str] = []
    seen: set[str] = set()
    for raw_text in texts:
        cleaned_raw = re.sub(r"\s+", " ", raw_text or "").strip()
        norm = _dedup_key(cleaned_raw)
        if len(norm) < 3:
            continue
        if len(norm) == 32 and all(ch in "0123456789abcdef" for ch in norm):
            continue
        if norm in {"download", "download options", "download mirror"}:
            continue
        if norm in seen:
            continue
        seen.add(norm)
        candidate = _clean_title_candidate(cleaned_raw)
        if candidate:
            candidates.append(candidate)
    return candidates


def _md5_links(element: Tag) -> list[Tag]:
    return [
        link for link in element.find_all("a", href=True) if MD5_PATH in _href(link)
    ]


def _href(link: Tag) -> str:
    value = link.get("href")
    return str(value).strip() if value else ""


def _title_candidates(cell: Tag) -> list[str]:
    """Return the title snippets of a table title cell, link text first."""
    snippets = [link.get_text(" ", strip=True) for link in _md5_links(cell)]
    full_text = cell.get_text(separator="\n", strip=True)
    if full_text:
        snippets.append(full_text)
        for part in re.split(r"[\n\r]+", full_text):
            snippets.append(part)
            snippets.extend(re.split(r"\s*[\|•·;︰：/]+\s*", part))
        if "|||" in full_text:
            snippets.extend(full_text.split("|||"))
    return _collect_title_candidates(snippets)


def _creators(cell: Tag) -> list[str]:
    """Return the creator names of a table creators cell."""
    raw_text = cell.get_text(separator=";", strip=True)
    creators: list[str] = []
    seen: set[str] = set()
    for part in re.split(r"[,;/\|]+", raw_text):
        candidate = re.sub(r"\s+", " ", part).strip()
        if len(candidate) < 2:
            continue
        norm = _dedup_key(candidate)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        creators.append(candidate)
    return creators


def _md5_of_href(href: str) -> str | None:
    """Return the lowercased MD5 of a ``/md5/<hash>`` link, or None."""
    parts = href.split(MD5_PATH)
    if len(parts) < 2:
        return None
    md5 = parts[1].split("?")[0].split("/")[0]
    if len(md5) != 32 or not set(md5) <= _HEX:
        return None
    return md5.lower()


def _record(
    base_url: str,
    md5: str,
    candidates: list[str],
    creators: list[str],
    date: str | None,
) -> SearchRecord:
    title = candidates[0] if candidates else ""
    if len(title) < 3 or title == md5:
        # An invented title ("Book <md5>") would be stored as the work's own
        # and judged like a real one; an empty title is dropped instead.
        title = ""
    return SearchRecord(
        provider=KEY,
        source_id=md5,
        title=title,
        creators=tuple(creators),
        date=date,
        year=parse_year(date),
        item_url=f"{base_url}{MD5_PATH}{md5}",
        raw=MappingProxyType({"md5": md5, "title_candidates": tuple(candidates)}),
    )


def _table_records(
    soup: BeautifulSoup, base_url: str, limit: int
) -> list[SearchRecord]:
    """Parse the table display: [icons, title, authors, publisher, year, file]."""
    records: list[SearchRecord] = []
    seen: set[str] = set()
    rows = soup.find_all("tr")
    for row in rows[1:]:
        if len(records) >= limit:
            break
        links = _md5_links(row)
        md5 = _md5_of_href(_href(links[0])) if links else None
        if md5 is None or md5 in seen:
            continue
        seen.add(md5)
        cells = row.find_all(["td", "th"])
        candidates = _title_candidates(cells[1]) if len(cells) > 1 else []
        creators = _creators(cells[2]) if len(cells) > 2 else []
        date = (cells[4].get_text(strip=True) or None) if len(cells) > 4 else None
        records.append(_record(base_url, md5, candidates, creators, date))
    return records


def _link_records(soup: BeautifulSoup, base_url: str, limit: int) -> list[SearchRecord]:
    """Parse any page by its ``/md5/`` links (when no table row matched)."""
    records: list[SearchRecord] = []
    seen: set[str] = set()
    for link in _md5_links(soup):
        if len(records) >= limit:
            break
        md5 = _md5_of_href(_href(link))
        if md5 is None or md5 in seen:
            continue
        seen.add(md5)
        title_attr = link.get("title")
        snippets = [link.get_text(" ", strip=True), str(title_attr or "")]
        parent = link.find_parent()
        if parent is not None:
            snippets.append(parent.get_text(" ", strip=True))
            snippets.extend(
                element.get_text(" ", strip=True)
                for element in parent.find_all(
                    ["div", "span", "h1", "h2", "h3", "h4", "p"]
                )
            )
        candidates = _collect_title_candidates(s for s in snippets if s)
        records.append(_record(base_url, md5, candidates, [], None))
    return records


def parse_search_page(html: str, *, base_url: str, limit: int) -> list[SearchRecord]:
    """Return at most *limit* records of a search page (``display=table``).

    Table rows are read first; a page without matching rows falls back to
    every ``/md5/`` link. ``item_url`` points to the MD5 page at *base_url*.
    """
    soup = BeautifulSoup(html, "html.parser")
    records = _table_records(soup, base_url, limit)
    return records or _link_records(soup, base_url, limit)


def is_annas_page(html: str) -> bool:
    """Report whether *html* is an Anna's Archive page (it names the site)."""
    return _BRAND.search(html) is not None


@dataclass(frozen=True)
class Md5Page:
    """An MD5 page: its metadata and its download links in priority order.

    Direct files come first, then slow downloads, then mirror pages.
    """

    metadata: Mapping[str, Any]
    links: tuple[str, ...]


def _link_kind(full_url: str, href: str, text: str) -> str | None:
    """Return ``slow``, ``direct`` or ``mirror`` for a download link, else None."""
    url_lower = full_url.lower()
    if "slow_download" in href or "slow download" in text:
        return "slow"
    if any(
        url_lower.endswith(ext) or f"{ext}?" in url_lower for ext in FILE_EXTENSIONS
    ):
        return "direct"
    if any(mirror in url_lower for mirror in _MIRROR_HOSTS):
        return "mirror"
    return None


def parse_md5_page(html: str, page_url: str, md5: str) -> Md5Page:
    """Return the metadata and download links of an MD5 page.

    Member-only fast downloads, in-page anchors and site links (account,
    login, donate, ...) are skipped; relative links resolve against the host
    of *page_url*. A fragment does not disqualify a link.
    """
    soup = BeautifulSoup(html, "html.parser")
    parts = urlsplit(page_url)
    page_base = f"{parts.scheme}://{parts.netloc}"
    metadata: dict[str, Any] = {"md5": md5, "url": page_url}
    title = soup.find("h1") or soup.find("div", class_="text-xl")
    if title is not None:
        metadata["title"] = title.get_text(strip=True)
    links: dict[str, list[str]] = {"direct": [], "slow": [], "mirror": []}
    for link in soup.find_all("a", href=True):
        href = _href(link)
        text = link.get_text(strip=True).lower()
        if not href or "fast_download" in href or "member" in text:
            continue
        href_lower = href.lower()
        if href_lower.startswith("#") or any(s in href_lower for s in _SKIPPED_LINKS):
            continue
        if href.startswith("/"):
            full_url = f"{page_base}{href}"
        elif href.startswith("http"):
            full_url = href
        else:
            continue
        kind = _link_kind(full_url, href, text)
        if kind is not None:
            links[kind].append(full_url)
    for script in soup.find_all("script"):
        for match in _SCRIPT_FILE_URL.finditer(script.string or ""):
            url = match.group(0).rstrip(",\"');")
            if url not in links["direct"]:
                links["direct"].append(url)
    ordered = links["direct"] + links["slow"] + links["mirror"]
    return Md5Page(MappingProxyType(metadata), tuple(ordered))


@dataclass(frozen=True)
class FastDownload:
    """What the fast-download API answered.

    ``download_url`` is None when it offers no fast download; ``error`` is
    its error text; ``quota_exhausted`` marks an error that names the quota
    or a limit; ``account`` is ``account_fast_download_info``.
    """

    download_url: str | None
    error: str = ""
    quota_exhausted: bool = False
    account: Mapping[str, Any] | None = None


def read_fast_download(document: Mapping[str, Any]) -> FastDownload:
    """Read a fast-download API answer."""
    account = document.get("account_fast_download_info")
    account_info = account if isinstance(account, Mapping) else None
    error = str(document.get("error") or "")
    if error:
        lowered = error.lower()
        exhausted = "quota" in lowered or "limit" in lowered
        return FastDownload(None, error, exhausted, account_info)
    url = document.get("download_url")
    return FastDownload(str(url) if url else None, account=account_info)


def login_page_reason(content_type: str, size: int, head: bytes) -> str:
    """Return why a downloaded body is Anna's Archive's login or error page.

    An HTML body whose first :data:`LOGIN_CHECK_CHARS` characters carry a
    :data:`LOGIN_MARKERS` entry, or an HTML body of about 180 KB (between
    the :data:`LOGIN_PAGE_BYTES` bounds), is one; '' means it is not.
    """
    lowered_type = content_type.lower()
    if extension_for_content_type(content_type) != ".html" and not (
        head.lstrip()[:15].lower().startswith((b"<!doctype html", b"<html"))
    ):
        return ""
    text = head.decode("utf-8", errors="ignore")[:LOGIN_CHECK_CHARS].lower()
    if any(marker in text for marker in LOGIN_MARKERS):
        return "Anna's Archive login or error page"
    low, high = LOGIN_PAGE_BYTES
    if "text/html" in lowered_type and low < size < high:
        return "Anna's Archive HTML page of about 180 KB (login or error page)"
    return ""


def _url_suffix(url: str) -> str:
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    return _SUFFIX_ALIASES.get(suffix, suffix)


def _read_head(path: Path) -> bytes:
    with path.open("rb") as handle:
        return handle.read(_HEAD_BYTES)


def _remove(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def _md5_of(record_or_id: SearchRecord | str) -> str:
    """Return the item's MD5, lowercased.

    Raises:
        ItemNotFound: The identifier is not a 32-digit hexadecimal hash.
    """
    value = source_id_of(record_or_id)
    if isinstance(record_or_id, SearchRecord):
        value = str(record_or_id.raw.get("md5") or value)
    md5 = value.strip()
    if md5.lower().startswith("md5/"):
        md5 = md5[4:]
    if len(md5) != 32 or not set(md5) <= _HEX:
        raise ItemNotFound(KEY, value)
    return md5.lower()


def _halt(exc: BreakerOpen | QuotaSpent | SlotLost) -> StopReason:
    if isinstance(exc, BreakerOpen):
        return "breaker_open"
    if isinstance(exc, QuotaSpent):
        return "quota_spent"
    return "slot_lost"


@dataclass
class _Run:
    """The state of one download call."""

    network: Network
    staging_dir: Path
    request: DownloadRequest
    files: list[StagedFile] = field(default_factory=list)
    stopped: StopReason | None = None
    quota_charged: bool = False
    refusal: str | None = None

    def admits(self, kind: FileKind) -> bool:
        budget = self.request.budget
        if budget is None or budget.allows(kind):
            return True
        if kind != "metadata":
            self.stopped = "budget"
        logger.warning("%s: size limit reached for %s files", KEY, kind)
        return False

    def keep(self, staged: StagedFile) -> None:
        self.files.append(staged)
        if self.request.budget is not None:
            self.request.budget.charge(staged.kind, staged.size)

    def metadata(self, name: str, document: Any, source_url: str) -> None:
        if self.request.include_metadata and self.admits("metadata"):
            self.keep(
                stage_metadata(self.staging_dir, name, document, source_url=source_url)
            )

    def result(self, md5: str, present: bool) -> DownloadResult:
        expected = (RENDERING_ITEM,)
        part = Completeness("pdf", expected, expected if present else (), available=1)
        return DownloadResult(
            provider=KEY,
            source_id=md5,
            format=self.request.format,
            parts=(part,),
            files=tuple(self.files),
            stopped=self.stopped,
            quota_charged=self.quota_charged,
        )


class AnnasArchive:
    """The Anna's Archive adapter.

    An item is one file, identified by its MD5 (``source_id``). Its download
    is the item's whole document, staged as ``rendering.pdf`` or
    ``rendering.epub`` and reported as the ``pdf`` part under ``--format
    pdf`` and ``both``; the library has no page images, so ``--format
    images`` and :meth:`pages` raise :class:`UnsupportedCapability`. With a
    member key the fast-download API is asked first (one quota unit per
    call when the quota is enabled); when it offers no file, the public
    links of the MD5 page are tried. The member key is optional, so
    ``capabilities.api_key`` is None.

    Raises (search, download): :class:`MirrorNotConfigured` while
    *base_url* is empty.

    ``hosts`` is empty: the mirror is configured per run, so no URL host
    is known to belong to this adapter.
    """

    hosts: ClassVar[frozenset[str]] = frozenset()

    def __init__(
        self, base_url: str, *, api_key: str | None = None, quota: bool = True
    ) -> None:
        self._base_url = base_url.strip().rstrip("/")
        self._api_key = api_key or None
        self._quota = quota
        self._capabilities = Capabilities(quota=quota and self._api_key is not None)

    def __repr__(self) -> str:
        member = self._api_key is not None
        return f"AnnasArchive(base_url={self._base_url!r}, member={member})"

    @property
    def key(self) -> str:
        """The provider key."""
        return KEY

    @property
    def capabilities(self) -> Capabilities:
        """What the adapter offers."""
        return self._capabilities

    @property
    def available(self) -> bool:
        """Return True when a mirror is configured."""
        return bool(self._base_url)

    def _base(self) -> str:
        if not self._base_url:
            raise MirrorNotConfigured()
        return self._base_url

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        """Search the mirror's table display, PDFs first.

        Raises:
            MirrorNotConfigured: No mirror is configured.
            ProviderBlocked: The request failed or the breaker is open.
            InvalidResponse: The page is not an Anna's Archive page.
        """
        base = self._base()
        if limit < 1:
            return []
        text = f"{query.title} {query.creator}" if query.creator else query.title
        url = f"{base}{SEARCH_PATH}"
        params = {"q": text, "display": "table", "ext": "pdf"}
        try:
            response = network.request(KEY, "GET", url, params=params)
        except RequestFailed as exc:
            raise ProviderBlocked(KEY, exc.cause.value, detail=str(exc)) from exc
        except BreakerOpen as exc:
            raise provider_error(KEY, exc) from exc
        html = _page_text(response)
        records = parse_search_page(html, base_url=base, limit=limit)
        if not records and not is_annas_page(html):
            raise InvalidResponse(
                KEY, url, "not an Anna's Archive page (parked or moved mirror?)"
            )
        logger.info("%s: %d results", KEY, len(records))
        return records

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        """Stage the item's file as ``rendering.<ext>``.

        Raises:
            MirrorNotConfigured: No mirror is configured.
            UnsupportedCapability: ``--format images`` was asked.
            ItemNotFound: The identifier is no MD5, or the MD5 page is gone.
            ProviderBlocked: The MD5 page could not be fetched.
            OSError: A file could not be written.
        """
        base = self._base()
        md5 = _md5_of(record_or_id)
        if request.format == "images":
            raise UnsupportedCapability(KEY, "images")
        run = _Run(network, staging_dir, request)
        if RENDERING_ITEM in request.present:
            return run.result(md5, present=True)
        staging_dir.mkdir(parents=True, exist_ok=True)
        if not run.admits("pdf"):
            return run.result(md5, present=False)
        staged: StagedFile | None = None
        try:
            if self._api_key is not None:
                staged = self._fast_download(run, base, md5, self._api_key)
            if staged is None and run.stopped is None:
                staged = self._public_download(run, base, md5)
        except (BreakerOpen, QuotaSpent, SlotLost) as exc:
            logger.warning("%s: download of %s stopped: %s", KEY, md5, exc)
            run.stopped = _halt(exc)
            run.quota_charged = run.quota_charged or exc.quota_charged
        except ProviderBlocked as exc:
            if run.refusal is None:
                raise
            raise ProviderBlocked(
                KEY,
                exc.cause,
                until=exc.until,
                detail=f"{exc.detail}; fast-download API refused: {run.refusal}",
            ) from exc
        return run.result(md5, present=staged is not None)

    def _fast_download(
        self, run: _Run, base: str, md5: str, api_key: str
    ) -> StagedFile | None:
        """Stage the file the fast-download API offers; None to fall back.

        A quota the server reports as spent sets ``quota_spent`` (no
        fallback). Whether the call stayed quota-charged is the network
        client's account: the response, or the exception's
        ``quota_charged``.

        Raises:
            BreakerOpen, QuotaSpent, SlotLost: The download must stop.
        """
        url = f"{base}{FAST_DOWNLOAD_PATH}"
        logger.info("%s: asking the fast-download API for %s", KEY, md5)
        try:
            response = run.network.request(
                KEY,
                "GET",
                url,
                params={"md5": md5, "key": api_key},
                slot=run.request.slot,
                quota=self._quota,
                final_statuses=_REFUSALS,
            )
        except RequestFailed as exc:
            run.quota_charged = run.quota_charged or exc.quota_charged
            if exc.status in _REFUSALS:
                said = exc.detail.replace(api_key, "***")
                run.refusal = f"HTTP {exc.status}: {said}".rstrip(": ")
            failure = str(exc).replace(api_key, "***")
            logger.warning("%s: fast-download API failed for %s: %s", KEY, md5, failure)
            return None
        run.quota_charged = run.quota_charged or self._quota
        answer = _fast_answer(response)
        if answer is None:
            logger.info("%s: no fast download offered for %s", KEY, md5)
            return None
        if answer.error:
            _fast_error(run, md5, answer)
            return None
        if answer.download_url is None:
            logger.info("%s: no fast download offered for %s", KEY, md5)
            if answer.account is not None:
                run.metadata("fast_download", _redacted(response), url)
            return None
        run.metadata("fast_download", _redacted(response), url)
        if answer.account is not None:
            logger.info(
                "%s: fast downloads left today: %s",
                KEY,
                answer.account.get("downloads_left", "unknown"),
            )
        try:
            return _stage_document(run, answer.download_url)
        except RequestFailed as exc:
            logger.warning("%s: fast download of %s failed: %s", KEY, md5, exc)
            return None

    def _public_download(self, run: _Run, base: str, md5: str) -> StagedFile | None:
        """Stage the first public link of the MD5 page that downloads.

        Raises:
            ItemNotFound, ProviderBlocked: The MD5 page could not be fetched.
            BreakerOpen, SlotLost: The download must stop.
        """
        page_url = f"{base}{MD5_PATH}{md5}"
        try:
            response = run.network.request(KEY, "GET", page_url, slot=run.request.slot)
        except RequestFailed as exc:
            raise provider_error(KEY, exc, md5) from exc
        page = parse_md5_page(_page_text(response), page_url, md5)
        run.metadata("md5_page", dict(page.metadata), page_url)
        if not page.links:
            logger.warning("%s: no download links on %s", KEY, page_url)
            return None
        for url in page.links[:MAX_LINK_ATTEMPTS]:
            try:
                return _stage_document(run, url)
            except RequestFailed as exc:
                logger.info("%s: link failed for %s: %s", KEY, md5, exc)
        logger.warning("%s: no link of %s could be downloaded", KEY, page_url)
        return None

    def pages(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        first_n: int,
        max_px: int,
    ) -> list[PageRef]:
        """Raise :class:`UnsupportedCapability`: items have no page images."""
        raise UnsupportedCapability(KEY, "pages")


def _page_text(response: requests.Response) -> str:
    """Return an HTML body as text, UTF-8 unless the response names a charset.

    Without a charset, requests would assume ISO-8859-1 for ``text/html``.
    """
    if "charset" in response.headers.get("Content-Type", "").lower():
        return response.text
    return response.content.decode("utf-8", errors="replace")


def _fast_answer(response: requests.Response) -> FastDownload | None:
    """Return the API's answer, or None for an empty or unreadable body."""
    if response.status_code == 204 or not response.content:
        return None
    try:
        document = response.json()
    except ValueError:
        logger.warning("%s: the fast-download API answered no JSON", KEY)
        return None
    if not isinstance(document, Mapping):
        return None
    return read_fast_download(document)


def _fast_error(run: _Run, md5: str, answer: FastDownload) -> None:
    """Log an API error; a spent quota stops the download."""
    if answer.quota_exhausted:
        logger.warning("%s: fast-download quota spent: %s", KEY, answer.error)
        run.stopped = "quota_spent"
        return
    lowered = answer.error.lower()
    if "invalid key" in lowered:
        logger.error("%s: the member key was refused; check its variable", KEY)
    elif "invalid md5" in lowered:
        logger.error("%s: the API refused the MD5 %s", KEY, md5)
    else:
        logger.warning("%s: fast-download API error: %s", KEY, answer.error)


def _redacted(response: requests.Response) -> dict[str, Any]:
    """Return the API answer with credentials in its download URL masked."""
    document = dict(response.json())
    url = document.get("download_url")
    if isinstance(url, str):
        document["download_url"] = redact_url(url)
    return document


def _stage_document(run: _Run, url: str) -> StagedFile:
    """Download *url* and stage it as ``rendering.pdf`` or ``rendering.epub``.

    Raises:
        RequestFailed: The download failed; a login or error page (cause
            ``blocked``) or a body that is no PDF or EPUB
            (``invalid_payload``) is discarded.
        BreakerOpen, QuotaSpent, SlotLost: The download must stop.
        OSError: The file could not be written.
    """
    outcome = run.network.download(
        KEY, url, run.staging_dir / f"{RENDERING_ITEM}.part", slot=run.request.slot
    )
    head = _read_head(outcome.path)
    reason = login_page_reason(outcome.content_type, outcome.size, head)
    if reason:
        _remove(outcome.path)
        raise RequestFailed(Cause.BLOCKED, outcome.url, detail=reason)
    extension = outcome.extension or _url_suffix(url)
    mismatch = (
        magic_mismatch(extension, head)
        if extension in DOCUMENT_EXTENSIONS
        else f"expected a PDF or EPUB, got {extension or 'an unknown type'}"
    )
    if mismatch:
        _remove(outcome.path)
        raise RequestFailed(Cause.INVALID_PAYLOAD, outcome.url, detail=mismatch)
    final = run.staging_dir / f"{RENDERING_ITEM}{extension}"
    replace_with_retry(final, outcome.path)
    staged = StagedFile(
        name=final.relative_to(run.staging_dir).as_posix(),
        kind="pdf",
        item=RENDERING_ITEM,
        size=outcome.size,
        content_type=outcome.content_type,
        source_url=outcome.url,
    )
    run.keep(staged)
    return staged


def build(settings: Settings) -> AnnasArchive:
    """Build the adapter from settings and the environment.

    Mirror and quota come from settings, the member key from the environment
    variable ``api_keys.annas_archive`` names.
    """
    block = settings.providers[KEY]
    base_url, quota = "", False
    if isinstance(block, AnnasArchiveSettings):
        base_url, quota = block.base_url, block.quota.enabled
    variable = settings.api_keys.get(KEY)
    api_key = os.environ.get(variable) if variable else None
    return AnnasArchive(base_url, api_key=api_key, quota=quota)
