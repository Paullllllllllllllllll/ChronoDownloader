"""A manifest's summary: label, page count and the renderings a download takes.

Ported from ChronoDownloader v1.30.3 ``api/iiif/_direct.py``
(``preview_manifest``) on 19.10.2026. Changed: the caller fetches the
manifest; the provider key and item identifier come from
:mod:`chronodownloader.iiif.urls`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from .manifest import extract_manifest_metadata, extract_page_sources
from .renderings import select_renderings

__all__ = ["ManifestSummary", "summarize_manifest"]


def _no_metadata() -> Mapping[str, str]:
    return MappingProxyType({})


@dataclass(frozen=True)
class ManifestSummary:
    """What a download of the manifest would see.

    ``page_count`` counts one source per canvas, service-backed or direct;
    ``rendering_formats`` lists the declared formats of the renderings a
    download would try, in its preference order.
    """

    label: str | None
    page_count: int
    has_renderings: bool
    rendering_formats: tuple[str, ...]
    metadata: Mapping[str, str] = field(default_factory=_no_metadata)


def summarize_manifest(manifest: Mapping[str, Any]) -> ManifestSummary:
    """Return the summary of a fetched manifest."""
    metadata = extract_manifest_metadata(manifest)
    selected = select_renderings(manifest)
    return ManifestSummary(
        label=metadata.label,
        page_count=len(extract_page_sources(manifest)),
        has_renderings=bool(selected),
        rendering_formats=tuple(item.format for item in selected if item.format),
        metadata=metadata.metadata,
    )
