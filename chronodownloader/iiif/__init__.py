"""IIIF support: manifests, canvases, renderings and Image API sizing.

Pure functions over fetched JSON: nothing here sends a request or keeps
state. The provider layer fetches manifests and ``info.json`` documents
through the run's network client and stages the files
(:mod:`chronodownloader.providers.iiif_download`).
"""

from __future__ import annotations

from .image_api import SPECULATIVE_CANDIDATES, image_url_candidates, info_url
from .manifest import (
    Canvas,
    ManifestMetadata,
    PageSource,
    extract_canvases,
    extract_direct_image_urls,
    extract_image_service_bases,
    extract_manifest_metadata,
    extract_page_sources,
    localized_text,
)
from .renderings import (
    DEFAULT_WHITELIST,
    MAX_RENDERING_ATTEMPTS,
    Rendering,
    collect_all_renderings,
    select_renderings,
)
from .summary import ManifestSummary, summarize_manifest
from .urls import (
    IIIF_MANIFEST_PATTERNS,
    detect_provider_from_url,
    extract_item_id_from_url,
    is_iiif_manifest_url,
)

__all__ = [
    "DEFAULT_WHITELIST",
    "IIIF_MANIFEST_PATTERNS",
    "MAX_RENDERING_ATTEMPTS",
    "SPECULATIVE_CANDIDATES",
    "Canvas",
    "ManifestMetadata",
    "ManifestSummary",
    "PageSource",
    "Rendering",
    "collect_all_renderings",
    "detect_provider_from_url",
    "extract_canvases",
    "extract_direct_image_urls",
    "extract_image_service_bases",
    "extract_item_id_from_url",
    "extract_manifest_metadata",
    "extract_page_sources",
    "image_url_candidates",
    "info_url",
    "is_iiif_manifest_url",
    "localized_text",
    "select_renderings",
    "summarize_manifest",
]
