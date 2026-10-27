"""The state of one download call, and helpers of the file-listing adapters.

:class:`DownloadRun` holds the state of one download call (staged files,
the size budget, why it stopped) and stages the metadata document and the
rendering. The IIIF manifest strategy
(:func:`~chronodownloader.providers.iiif_download.download_manifest`) uses
it, and so do the Internet Archive and Google Books adapters, which list an
item's whole-document files and page images themselves.
:func:`wants_images` applies the ``--format`` rule (decision D2);
:func:`get_json` fetches a JSON object with query parameters, so that a key
passed as a parameter never appears in an error's URL.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from chronodownloader.coord import SlotLost, SlotToken
from chronodownloader.iiif import MAX_RENDERING_ATTEMPTS, Rendering
from chronodownloader.net import BreakerOpen, Network, QuotaSpent, RequestFailed
from chronodownloader.net.credentials import redact_url

from .base import (
    RENDERING_ITEM,
    Completeness,
    DownloadFormat,
    DownloadRequest,
    DownloadResult,
    FileKind,
    InvalidResponse,
    StagedFile,
    StopReason,
    provider_error,
)
from .staging import serving, stage_download, stage_metadata

__all__ = ["DownloadRun", "get_json", "wants_images"]

logger = logging.getLogger(__name__)

type Halt = BreakerOpen | QuotaSpent | SlotLost


def get_json(
    network: Network,
    provider: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    slot: SlotToken | None = None,
    item: str = "",
) -> Mapping[str, Any]:
    """Fetch a JSON object; *params* are sent as the query string.

    Raises:
        ItemNotFound: The provider answered 404 or 410 (for *item*).
        ProviderBlocked: The request failed otherwise, the breaker is open,
            the quota is spent or the slot was lost.
        InvalidResponse: The body is not a JSON object.
    """
    try:
        with serving(network, url, provider, slot) as key:
            response = network.request(key, "GET", url, params=params, slot=slot)
    except (RequestFailed, BreakerOpen, QuotaSpent, SlotLost) as exc:
        raise provider_error(provider, exc, item or redact_url(url)) from exc
    try:
        document = response.json()
    except ValueError as exc:
        raise InvalidResponse(provider, redact_url(url), "not JSON") from exc
    if not isinstance(document, Mapping):
        raise InvalidResponse(provider, redact_url(url), "not a JSON object")
    return document


def wants_images(
    fmt: DownloadFormat, *, offered: bool, rendering_failed: bool, has_pages: bool
) -> bool:
    """Return True when a download of *fmt* reports the page images.

    ``images`` always does; ``both`` when the item has pages or offers no
    rendering; ``pdf`` when no rendering is offered or the offered one could
    not be fetched.
    """
    if fmt == "images":
        return True
    if fmt == "both":
        return has_pages or not offered
    return not offered or rendering_failed


def _halt_reason(exc: Halt) -> StopReason:
    if isinstance(exc, BreakerOpen):
        return "breaker_open"
    if isinstance(exc, QuotaSpent):
        return "quota_spent"
    return "slot_lost"


@dataclass
class DownloadRun:
    """The state of one download call: staged files and why it stopped."""

    network: Network
    provider: str
    staging_dir: Path
    request: DownloadRequest
    files: list[StagedFile] = field(default_factory=list)
    halt: StopReason | None = None
    over_budget: bool = False

    @property
    def slot(self) -> SlotToken:
        return self.request.slot

    @property
    def stopped(self) -> StopReason | None:
        """Return why the download ended early, or None."""
        return self.halt or ("budget" if self.over_budget else None)

    def admits(self, kind: FileKind) -> bool:
        """Return True when the budget allows another file of *kind*."""
        budget = self.request.budget
        if budget is None or budget.allows(kind):
            return True
        if kind != "metadata":
            self.over_budget = True
        logger.warning("%s: size limit reached for %s files", self.provider, kind)
        return False

    def keep(self, staged: StagedFile) -> None:
        """Record a staged file and charge it to the budget."""
        self.files.append(staged)
        if self.request.budget is not None:
            self.request.budget.charge(staged.kind, staged.size)

    def stop(self, exc: Halt) -> None:
        """End the download for an open breaker, a spent quota or a lost slot."""
        logger.warning("%s: download stopped: %s", self.provider, exc)
        self.halt = _halt_reason(exc)

    def stage_metadata(
        self, name: str, document: Mapping[str, Any], *, source_url: str = ""
    ) -> None:
        """Stage *document* as ``metadata/<name>.json``.

        Nothing is staged unless the request includes metadata and the budget
        allows it.

        Raises:
            OSError: The file could not be written.
        """
        if self.request.include_metadata and self.admits("metadata"):
            self.keep(
                stage_metadata(self.staging_dir, name, document, source_url=source_url)
            )

    def stage_rendering(self, candidates: Sequence[Rendering]) -> Completeness:
        """Stage the first of *candidates* that downloads as the rendering.

        At most :data:`~chronodownloader.iiif.MAX_RENDERING_ATTEMPTS` are
        tried; a failed candidate moves on to the next one. When none
        arrives, the last failure's cause is the rendering's cause.

        Raises:
            OSError: The file could not be written.
        """
        expected = (RENDERING_ITEM,)
        if RENDERING_ITEM in self.request.present:
            return Completeness("pdf", expected, expected, available=1)
        if not self.admits("pdf"):
            return Completeness("pdf", expected, (), available=1)
        causes: dict[str, str] = {}
        for rendering in candidates[:MAX_RENDERING_ATTEMPTS]:
            try:
                staged = stage_download(
                    self.network,
                    self.provider,
                    rendering.url,
                    self.staging_dir,
                    RENDERING_ITEM,
                    slot=self.slot,
                    kind="pdf",
                )
            except RequestFailed as exc:
                logger.warning("%s: rendering failed: %s", self.provider, exc)
                causes[RENDERING_ITEM] = exc.cause.value
                continue
            except (BreakerOpen, QuotaSpent, SlotLost) as exc:
                self.stop(exc)
                break
            self.keep(
                replace(
                    staged,
                    label=rendering.label,
                    declared_format=rendering.format or None,
                )
            )
            return Completeness("pdf", expected, expected, available=1)
        return Completeness("pdf", expected, (), available=1, causes=causes)

    def result(self, source_id: str, parts: Sequence[Completeness]) -> DownloadResult:
        """Return the download's result with *parts*."""
        return DownloadResult(
            provider=self.provider,
            source_id=source_id,
            format=self.request.format,
            parts=tuple(parts),
            files=tuple(self.files),
            stopped=self.stopped,
        )
