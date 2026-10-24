"""Sample an item's first pages at a reduced size, for ``inspect`` and ``verify``.

:func:`sample_pages` asks the provider for its first canvases
(:meth:`~chronodownloader.providers.base.Provider.pages`) at the smaller of
the requested size and the provider's image size cap, then downloads each
page into the destination folder as ``p00001.jpg`` and so on. A page with an
Image API service falls back to the sizes its ``info.json`` advertises,
never above the cap; a page that cannot be had is reported, not raised. An
open breaker, a spent quota or a lost slot ends the sample early.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from chronodownloader.coord import SlotLost, SlotToken
from chronodownloader.net import BreakerOpen, Network, QuotaSpent
from chronodownloader.providers.base import (
    PageRef,
    Provider,
    SearchRecord,
    StagedFile,
    StopReason,
    UnsupportedCapability,
    source_id_of,
)
from chronodownloader.providers.iiif_download import stage_service_image
from chronodownloader.providers.staging import stage_first

__all__ = ["PageSample", "SampledPage", "sample_pages"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SampledPage:
    """One sampled canvas; ``file`` is None when no URL could be fetched."""

    item: str
    index: int
    label: str | None
    file: StagedFile | None

    @property
    def ok(self) -> bool:
        return self.file is not None


@dataclass(frozen=True)
class PageSample:
    """The pages sampled from one item, in canvas order.

    ``dest_dir`` holds the files; ``max_px`` is the size that applied;
    ``stopped`` says why the sample ended early, if it did.
    """

    provider: str
    source_id: str
    dest_dir: Path
    max_px: int
    pages: tuple[SampledPage, ...]
    stopped: StopReason | None = None

    @property
    def sampled(self) -> tuple[SampledPage, ...]:
        """Return the pages that were fetched."""
        return tuple(page for page in self.pages if page.ok)


def _effective_px(provider: Provider, max_px: int) -> int:
    cap = provider.capabilities.image_max_px
    return min(max_px, cap) if cap else max_px


def _fetch(
    network: Network,
    provider: str,
    ref: PageRef,
    dest_dir: Path,
    *,
    slot: SlotToken,
    max_px: int,
) -> StagedFile | None:
    if ref.service is not None:
        return stage_service_image(
            network, provider, ref.service, dest_dir, ref.item, slot=slot, max_px=max_px
        )
    staged = stage_first(
        network, provider, ref.urls, dest_dir, ref.item, slot=slot, kind="image"
    )
    return staged if isinstance(staged, StagedFile) else None


def sample_pages(
    provider: Provider,
    record_or_id: SearchRecord | str,
    *,
    network: Network,
    slot: SlotToken,
    dest_dir: Path,
    first_n: int,
    max_px: int,
) -> PageSample:
    """Download the first *first_n* pages of an item at most *max_px* pixels.

    Raises:
        ValueError: *first_n* or *max_px* is below 1.
        UnsupportedCapability: The provider is metadata-only.
        ProviderError: The provider could not list the pages.
        OSError: A file could not be written.
    """
    if first_n < 1 or max_px < 1:
        raise ValueError("first_n and max_px must be at least 1")
    if not provider.capabilities.downloads:
        raise UnsupportedCapability(provider.key, "pages")
    size = _effective_px(provider, max_px)
    refs = provider.pages(record_or_id, network=network, first_n=first_n, max_px=size)
    dest_dir.mkdir(parents=True, exist_ok=True)
    pages: list[SampledPage] = []
    stopped: StopReason | None = None
    for ref in refs[:first_n]:
        staged: StagedFile | None = None
        try:
            staged = _fetch(
                network, provider.key, ref, dest_dir, slot=slot, max_px=size
            )
        except BreakerOpen:
            stopped = "breaker_open"
        except QuotaSpent:
            stopped = "quota_spent"
        except SlotLost:
            stopped = "slot_lost"
        if stopped is not None:
            logger.warning("%s: page sample stopped (%s)", provider.key, stopped)
            break
        if staged is None:
            logger.warning("%s: page %s could not be sampled", provider.key, ref.item)
        pages.append(SampledPage(ref.item, ref.index, ref.label, staged))
    return PageSample(
        provider=provider.key,
        source_id=source_id_of(record_or_id),
        dest_dir=dest_dir,
        max_px=size,
        pages=tuple(pages),
        stopped=stopped,
    )
