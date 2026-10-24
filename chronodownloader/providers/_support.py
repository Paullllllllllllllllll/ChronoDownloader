"""Helpers of the Europeana, DPLA, DDB and Library of Congress adapters.

These adapters reach an item through a record or item document before any
IIIF manifest. :class:`Collector` stages that document as metadata under the
size budget, takes in the manifest download's result and, when the item
yielded no page and no rendering, stages a preview image (a thumbnail or a
representative image) as ``metadata/preview.<ext>``. A preview is not the
work: it never counts toward completeness, so a download that found only a
preview reports the work missing (:func:`missing_work`). v1.30.3 recorded
such a thumbnail as a completed download (DPLA, 19.10.2026 baseline).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from chronodownloader.coord import SlotLost, SlotToken
from chronodownloader.net import BreakerOpen, Network, QuotaSpent, RequestFailed
from chronodownloader.net.credentials import redact_url

from .base import (
    Completeness,
    DownloadRequest,
    DownloadResult,
    InvalidResponse,
    StagedFile,
    StopReason,
    provider_error,
)
from .staging import METADATA_DIR, serving, stage_download, stage_metadata

__all__ = [
    "PREVIEW_ITEM",
    "Collector",
    "as_creators",
    "fetch_document",
    "has_content",
    "missing_work",
]

logger = logging.getLogger(__name__)

PREVIEW_ITEM = "preview"
"""The metadata name of a staged preview image."""


def as_creators(value: Any) -> tuple[str, ...]:
    """Return a creator field as names, as v1.30.3 ``api.model._as_list`` did.

    A string is split on ``;`` (the Dublin Core and MARC separator) and never
    on ``,``, which separates an inverted personal name; a list keeps every
    element that is not None.
    """
    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(str(item) for item in value if item is not None)
    if isinstance(value, str):
        if ";" in value:
            return tuple(part.strip() for part in value.split(";") if part.strip())
        return (value,) if value.strip() else ()
    return (str(value),)


def fetch_document(
    network: Network,
    provider: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    slot: SlotToken | None = None,
    item: str = "",
) -> Any:
    """Fetch and parse a JSON document (a search response, an item record).

    Raises:
        ItemNotFound: The provider answered 404 or 410.
        ProviderBlocked: The request failed otherwise, the breaker is open,
            the quota is spent or the slot was lost.
        InvalidResponse: The body is not JSON.
    """
    try:
        with serving(network, url, provider, slot) as key:
            response = network.request(
                key, "GET", url, params=params, headers=headers, slot=slot
            )
    except (RequestFailed, BreakerOpen, QuotaSpent, SlotLost) as exc:
        raise provider_error(provider, exc, item or redact_url(url)) from exc
    try:
        return response.json()
    except ValueError as exc:
        raise InvalidResponse(provider, redact_url(url), "not JSON") from exc


def missing_work() -> tuple[Completeness, ...]:
    """Return the parts of an item that offered no page and no rendering."""
    return (Completeness("images", (), ()),)


def has_content(result: DownloadResult) -> bool:
    """Return True when the result holds a page or a rendering of the work."""
    return any(part.present for part in result.parts)


def _stop_reason(exc: BreakerOpen | QuotaSpent | SlotLost) -> StopReason:
    if isinstance(exc, BreakerOpen):
        return "breaker_open"
    if isinstance(exc, QuotaSpent):
        return "quota_spent"
    return "slot_lost"


@dataclass
class Collector:
    """The files one download stages around its manifest download."""

    network: Network
    provider: str
    staging_dir: Path
    request: DownloadRequest
    files: list[StagedFile] = field(default_factory=list)
    stopped: StopReason | None = None

    def _admits_metadata(self) -> bool:
        budget = self.request.budget
        return self.request.include_metadata and (
            budget is None or budget.allows("metadata")
        )

    def _keep(self, staged: StagedFile) -> None:
        self.files.append(staged)
        if self.request.budget is not None:
            self.request.budget.charge(staged.kind, staged.size)

    def document(self, name: str, document: Any, *, source_url: str = "") -> None:
        """Stage *document* as ``metadata/<name>.json`` when metadata is wanted.

        Raises:
            OSError: The file could not be written.
        """
        if self._admits_metadata():
            self.staging_dir.mkdir(parents=True, exist_ok=True)
            self._keep(
                stage_metadata(
                    self.staging_dir, name, document, source_url=redact_url(source_url)
                )
            )

    def absorb(self, result: DownloadResult) -> None:
        """Take in the files and the stop reason of a manifest download."""
        self.files.extend(result.files)
        self.stopped = self.stopped or result.stopped

    def optional_document(
        self, url: str, *, params: Mapping[str, Any] | None = None
    ) -> Any:
        """Fetch a JSON document only a preview depends on; None when it fails.

        A failed request is logged (the result already reports the work
        missing); an open breaker, a spent quota or a lost slot stops the
        download.
        """
        slot = self.request.slot
        try:
            with serving(self.network, url, self.provider, slot) as key:
                response = self.network.request(
                    key, "GET", url, params=params, slot=slot
                )
        except RequestFailed as exc:
            logger.info("%s: %s", self.provider, exc)
            return None
        except (BreakerOpen, QuotaSpent, SlotLost) as exc:
            self.stopped = _stop_reason(exc)
            return None
        try:
            return response.json()
        except ValueError:
            logger.info("%s: %s is not JSON", self.provider, redact_url(url))
            return None

    def preview(self, urls: Iterable[str | None]) -> None:
        """Stage the first of *urls* that downloads as an image preview.

        Every URL is tried in order; a failed one moves on to the next. The
        preview is metadata: it never counts as a page of the work.

        Raises:
            OSError: The file could not be written.
        """
        if self.stopped is not None:
            return
        tried: set[str] = set()
        for url in urls:
            if not url or url in tried or not self._admits_metadata():
                continue
            tried.add(url)
            try:
                staged = stage_download(
                    self.network,
                    self.provider,
                    url,
                    self.staging_dir,
                    f"{METADATA_DIR}/{PREVIEW_ITEM}",
                    slot=self.request.slot,
                    kind="image",
                )
            except RequestFailed as exc:
                logger.info("%s: preview %s failed: %s", self.provider, url, exc)
                continue
            except (BreakerOpen, QuotaSpent, SlotLost) as exc:
                self.stopped = _stop_reason(exc)
                return
            logger.info(
                "%s: staged a preview image only; the work is missing", self.provider
            )
            self._keep(replace(staged, kind="metadata", item=PREVIEW_ITEM))
            return

    def result(self, source_id: str, parts: tuple[Completeness, ...]) -> DownloadResult:
        """Return the download's result over every file staged so far."""
        return DownloadResult(
            provider=self.provider,
            source_id=source_id,
            format=self.request.format,
            parts=parts or missing_work(),
            files=tuple(self.files),
            stopped=self.stopped,
        )
