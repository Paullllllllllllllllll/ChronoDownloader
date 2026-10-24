"""The provider contract: protocol, capabilities, records, requests, results.

Every library adapter is a class implementing :class:`Provider`: ``search``
returns typed :class:`SearchRecord` values, ``download`` stages an item's
files and returns a :class:`DownloadResult`, and ``pages`` lists the first
canvases at a reduced size for ``inspect`` and ``verify``. Each adapter
declares its :class:`Capabilities`; a metadata-only adapter raises
:class:`UnsupportedCapability` from ``download`` and ``pages``. Every request
goes through the run's :class:`~chronodownloader.net.Network` under the
adapter's key.

Staging. ``download`` writes only into the staging folder its caller passes
(``objects/.staging/<generation>/`` of the work): page images as
``p00001.jpg`` and so on (the 1-based canvas position), a whole-document
rendering as ``rendering.pdf`` or ``rendering.epub``, metadata under
``metadata/``. Providers never promote files and never import the output or
matching layers; the lease-checked commit moves the staged files.

Completeness. The result holds one :class:`Completeness` per part of the
requested format (``--format``, decision D2): ``images`` lists the page
items within the page limit, ``pdf`` the single rendering item. ``pdf``
takes the item's rendering when it offers one, else its page images; when
an offered rendering cannot be fetched the adapter falls back to the page
images and both parts are reported, so the work stays partial until the
rendering arrives. ``both`` takes the rendering (when offered) and the page
images. A part is complete when it expects at least one item and every
expected item is present; the result is complete when it has parts and all
are. Items the request names as already present are skipped and count as
present (resume). A download stopped by the size budget, an open breaker, a
spent quota or a lost slot returns what it staged, with ``stopped`` set.

Errors. ``search``, ``download`` and ``pages`` raise :class:`ItemNotFound`,
:class:`ProviderBlocked` (with its cause), :class:`MissingApiKey`,
:class:`InvalidResponse` or :class:`UnsupportedCapability`; every one
derives from :class:`ProviderError`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal, Protocol

from chronodownloader.coord import SlotLost, SlotToken
from chronodownloader.net import BreakerOpen, Cause, QuotaSpent, RequestFailed

if TYPE_CHECKING:
    from chronodownloader.net import Network

__all__ = [
    "FORMATS",
    "RENDERING_ITEM",
    "Budget",
    "Capabilities",
    "Completeness",
    "DownloadFormat",
    "DownloadRequest",
    "DownloadResult",
    "FileKind",
    "InvalidResponse",
    "ItemNotFound",
    "MissingApiKey",
    "PageRef",
    "Part",
    "Provider",
    "ProviderBlocked",
    "ProviderError",
    "SearchQuery",
    "SearchRecord",
    "StagedFile",
    "StopReason",
    "UnsupportedCapability",
    "item_id_from_url",
    "item_manifest_url",
    "manifest_url",
    "page_item",
    "page_limit",
    "provider_error",
    "source_id_of",
]

type DownloadFormat = Literal["images", "pdf", "both"]
"""What a download fetches (``--format``)."""

FORMATS: tuple[DownloadFormat, ...] = ("images", "pdf", "both")

type Part = Literal["images", "pdf"]
"""A part of an item: its page images or its whole-document rendering."""

type FileKind = Literal["image", "pdf", "metadata"]
"""The size-limit class of a staged file; EPUB renderings count as ``pdf``."""

type StopReason = Literal["budget", "breaker_open", "quota_spent", "slot_lost"]
"""Why a download ended before every expected item was tried."""

RENDERING_ITEM = "rendering"
"""The item identifier of an item's whole-document rendering."""


def page_item(index: int) -> str:
    """Return the item identifier of the canvas at 1-based *index*."""
    return f"p{index:05d}"


def page_limit(requested: int, provider_max: int) -> int:
    """Return the page limit that applies: the smaller nonzero of the run's
    ``--max-pages`` and the provider's ``max_pages``; 0 for none."""
    limits = [value for value in (requested, provider_max) if value > 0]
    return min(limits) if limits else 0


@dataclass(frozen=True)
class Capabilities:
    """What an adapter offers.

    ``iiif``: items come as IIIF manifests. ``quota``: downloads are charged
    to a daily quota. ``api_key``: the service in ``settings.api_keys``
    whose key the adapter needs, or None. ``image_max_px``: the largest
    image dimension the library serves, or None. ``downloads``: False for a
    metadata-only adapter.
    """

    iiif: bool = False
    quota: bool = False
    api_key: str | None = None
    image_max_px: int | None = None
    downloads: bool = True


@dataclass(frozen=True)
class SearchQuery:
    """A work to search for; every field but ``title`` is optional."""

    title: str
    creator: str | None = None
    year: int | None = None
    place: str | None = None
    publisher: str | None = None
    language: str | None = None
    edition: str | None = None


def _no_raw() -> Mapping[str, Any]:
    return MappingProxyType({})


def _no_causes() -> Mapping[str, str]:
    return MappingProxyType({})


@dataclass(frozen=True)
class SearchRecord:
    """One candidate a provider's search returned.

    ``source_id`` identifies the item at the provider; ``date`` is the date
    as the provider gives it, ``year`` the year parsed from it; ``raw``
    keeps the provider's own fields for its download.
    """

    provider: str
    source_id: str
    title: str
    creators: tuple[str, ...] = ()
    date: str | None = None
    year: int | None = None
    place: str | None = None
    publisher: str | None = None
    language: str | None = None
    item_url: str | None = None
    iiif_manifest: str | None = None
    raw: Mapping[str, Any] = field(default_factory=_no_raw, hash=False)


def source_id_of(record_or_id: SearchRecord | str) -> str:
    """Return the item identifier of a record or of a bare identifier."""
    if isinstance(record_or_id, SearchRecord):
        return record_or_id.source_id
    return record_or_id


class Budget(Protocol):
    """The run's size limits as a download sees them."""

    def allows(self, kind: FileKind) -> bool:
        """Return True when another file of *kind* may be downloaded."""
        ...

    def charge(self, kind: FileKind, size: int) -> None:
        """Count a staged file of *kind* and *size* bytes."""
        ...


@dataclass(frozen=True)
class DownloadRequest:
    """What to download and under which slot.

    ``slot`` is the provider download slot every request names (the caller
    keeps it renewed). ``max_pages`` is the page limit (0 = none), already
    combined with the provider's (:func:`page_limit`); it binds page images
    only. ``present`` names the items the work already holds; they are
    skipped and count as present. ``budget`` is consulted before and charged
    after every file.

    Raises:
        ValueError: An unknown ``format`` or a negative ``max_pages``.
    """

    slot: SlotToken
    format: DownloadFormat = "pdf"
    max_pages: int = 0
    include_metadata: bool = True
    present: frozenset[str] = frozenset()
    budget: Budget | None = None

    def __post_init__(self) -> None:
        if self.format not in FORMATS:
            raise ValueError(f"unknown format {self.format!r}")
        if self.max_pages < 0:
            raise ValueError(f"max_pages must not be negative, got {self.max_pages}")

    @property
    def page_limit(self) -> int | None:
        """Return the page limit, or None when there is none."""
        return self.max_pages or None


@dataclass(frozen=True)
class StagedFile:
    """A file a download wrote into the staging folder.

    ``name`` is its path relative to the staging folder (forward slashes);
    ``item`` the expected item it satisfies (``p00001``, ``rendering``) or
    the metadata document's name; ``source_url`` has credentials masked.
    """

    name: str
    kind: FileKind
    item: str
    size: int
    content_type: str
    source_url: str
    label: str | None = None
    declared_format: str | None = None


@dataclass(frozen=True)
class Completeness:
    """Expected and present items of one part of a download.

    ``expected`` lists the items within the page limit, in order;
    ``page_limit`` is the limit applied (None without one) and ``available``
    the number of items the source offers before it. ``causes`` maps a
    missing item whose request failed to the failure's
    :class:`~chronodownloader.net.Cause` value (``blocked``, ``transient``,
    ``not_found``, ...); items left untried, as after a stop, have none.

    Raises:
        ValueError: ``present`` names an item that is not expected, or
            ``causes`` an item that is not missing.
    """

    part: Part
    expected: tuple[str, ...]
    present: tuple[str, ...]
    page_limit: int | None = None
    available: int = 0
    causes: Mapping[str, str] = field(default_factory=_no_causes, hash=False)

    def __post_init__(self) -> None:
        extra = set(self.present) - set(self.expected)
        if extra:
            raise ValueError(f"present items not expected: {sorted(extra)}")
        stray = set(self.causes) - set(self.missing)
        if stray:
            raise ValueError(f"causes for items not missing: {sorted(stray)}")
        object.__setattr__(self, "causes", MappingProxyType(dict(self.causes)))

    @property
    def missing(self) -> tuple[str, ...]:
        """Return the expected items that are not present, in order."""
        present = set(self.present)
        return tuple(item for item in self.expected if item not in present)

    @property
    def complete(self) -> bool:
        """Return True when items are expected and all of them are present."""
        return bool(self.expected) and not self.missing

    @property
    def truncated(self) -> bool:
        """Return True when the page limit left items of the source out."""
        return self.page_limit is not None and self.available > self.page_limit


@dataclass(frozen=True)
class DownloadResult:
    """What a download staged and how complete the item is.

    ``parts`` holds one :class:`Completeness` per part of the requested
    format; ``files`` every staged file, metadata included; ``stopped`` why
    the download ended early, if it did; ``quota_charged`` whether a
    quota-charged request was sent.
    """

    provider: str
    source_id: str
    format: DownloadFormat
    parts: tuple[Completeness, ...]
    files: tuple[StagedFile, ...] = ()
    stopped: StopReason | None = None
    quota_charged: bool = False

    @property
    def complete(self) -> bool:
        """Return True when every part of the requested format is complete."""
        return bool(self.parts) and all(part.complete for part in self.parts)

    def part(self, name: Part) -> Completeness | None:
        """Return the completeness of part *name*, or None when not reported."""
        return next((part for part in self.parts if part.part == name), None)


@dataclass(frozen=True)
class PageRef:
    """A canvas to sample: its item, 1-based position and label, the image
    URLs at the reduced size (preferred first), and its Image API service
    base when it has one (for the ``info.json`` fallback)."""

    item: str
    index: int
    urls: tuple[str, ...]
    label: str | None = None
    service: str | None = None


class ProviderError(Exception):
    """Base of the errors an adapter raises; ``provider`` is its key."""

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider


class UnsupportedCapability(ProviderError):
    """The adapter does not offer *capability* (``download``, ``pages``),
    for every item or, with *detail* saying why, for this one."""

    def __init__(self, provider: str, capability: str, *, detail: str = "") -> None:
        text = f"{capability} is not supported"
        super().__init__(provider, f"{text}: {detail}" if detail else text)
        self.capability = capability
        self.detail = detail


class ItemNotFound(ProviderError):
    """The provider does not know the item; ``url`` is the request, masked."""

    def __init__(self, provider: str, item: str, url: str = "") -> None:
        super().__init__(provider, f"item {item!r} not found")
        self.item = item
        self.url = url


class ProviderBlocked(ProviderError):
    """The provider refused or could not serve a request.

    ``cause`` is a :class:`~chronodownloader.net.Cause` value (``blocked``,
    ``transient``, ``dns``, ``ssl``, ``rejected``, ...) or one of
    ``breaker_open``, ``quota_spent`` and ``slot_lost``; ``until`` is when an
    open breaker or a spent quota admits requests again, if known.
    """

    def __init__(
        self,
        provider: str,
        cause: str,
        *,
        until: float | None = None,
        detail: str = "",
    ) -> None:
        text = f"blocked ({cause})"
        super().__init__(provider, f"{text}: {detail}" if detail else text)
        self.cause = cause
        self.until = until
        self.detail = detail


class MissingApiKey(ProviderError):
    """The adapter needs an API key and *env_var*, which names it, is unset."""

    def __init__(self, provider: str, env_var: str) -> None:
        super().__init__(provider, f"API key missing: {env_var} is unset")
        self.env_var = env_var


class InvalidResponse(ProviderError):
    """The provider answered with something that cannot be parsed."""

    def __init__(self, provider: str, url: str, detail: str) -> None:
        super().__init__(provider, f"invalid response from {url}: {detail}")
        self.url = url
        self.detail = detail


def provider_error(
    provider: str,
    error: RequestFailed | BreakerOpen | QuotaSpent | SlotLost,
    item: str = "",
) -> ProviderError:
    """Return the provider error that stands for a failed request.

    A 404 or 410 becomes :class:`ItemNotFound` for *item*; every other
    failure becomes :class:`ProviderBlocked` with its cause.
    """
    if isinstance(error, RequestFailed):
        if error.cause is Cause.NOT_FOUND:
            return ItemNotFound(provider, item, error.url)
        return ProviderBlocked(provider, error.cause.value, detail=str(error))
    if isinstance(error, BreakerOpen):
        return ProviderBlocked(
            provider, "breaker_open", until=error.until, detail=str(error)
        )
    if isinstance(error, QuotaSpent):
        return ProviderBlocked(provider, "quota_spent", until=error.status.reset_at)
    return ProviderBlocked(provider, "slot_lost", detail=str(error))


def manifest_url(provider: object, url: str) -> str:
    """Return the URL to fetch the IIIF manifest at *url* from, for *provider*.

    The optional adapter hook ``manifest_url(url) -> str`` adds what the
    library needs to serve the manifest (Europeana appends its key on its
    own manifest host); an adapter without the hook fetches *url* as it is.
    """
    hook = getattr(provider, "manifest_url", None)
    if hook is None:
        return url
    authorized: str = hook(url)
    return authorized


def item_id_from_url(provider: object, url: str) -> str | None:
    """Return the identifier of the item a library URL names, or None.

    The optional adapter hook ``item_id_from_url(url) -> str | None`` reads
    the library's item, viewer and manifest URL shapes (ported from v1.30.3
    ``api/identifier_resolver.py`` and ``api/iiif/_direct.py``); an adapter
    without the hook resolves no URL.
    """
    hook = getattr(provider, "item_id_from_url", None)
    if hook is None:
        return None
    found: str | None = hook(url.strip())
    return found or None


def item_manifest_url(provider: object, item_id: str) -> str | None:
    """Return the IIIF manifest URL of *provider*'s item *item_id*, or None.

    The optional adapter hook ``item_manifest_url(item_id) -> str | None``
    builds it from the library's manifest address (v1.30.3
    ``MANIFEST_TEMPLATES``); an adapter without the hook names none.
    """
    hook = getattr(provider, "item_manifest_url", None)
    if hook is None:
        return None
    found: str | None = hook(item_id)
    return found or None


class Provider(Protocol):
    """A library adapter.

    Besides the members below, an adapter may define the optional hooks
    ``manifest_url(url) -> str`` (see :func:`manifest_url`),
    ``item_id_from_url(url) -> str | None`` (see :func:`item_id_from_url`)
    and ``item_manifest_url(item_id) -> str | None`` (see
    :func:`item_manifest_url`).
    """

    @property
    def key(self) -> str:
        """The provider key, one of the 17 settings keys."""
        ...

    @property
    def capabilities(self) -> Capabilities:
        """What the adapter offers."""
        ...

    def search(
        self, query: SearchQuery, *, network: Network, limit: int
    ) -> list[SearchRecord]:
        """Return at most *limit* candidates for *query*, best first."""
        ...

    def download(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        staging_dir: Path,
        request: DownloadRequest,
    ) -> DownloadResult:
        """Stage the item's files in *staging_dir* and report completeness."""
        ...

    def pages(
        self,
        record_or_id: SearchRecord | str,
        *,
        network: Network,
        first_n: int,
        max_px: int,
    ) -> list[PageRef]:
        """Return the first *first_n* canvases at most *max_px* pixels."""
        ...
