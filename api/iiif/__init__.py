"""IIIF (International Image Interoperability Framework) support.

Consolidates all IIIF-related concerns previously split across api/iiif.py,
api/direct_iiif_api.py, api/download_helpers.py, and the iiif-rendering
helper in api/utils.py. Downstream consumers should import exclusively from
this package.

Public surface:

- Manifest parsing: extract_image_service_bases, extract_direct_image_urls,
  image_url_candidates, download_one_from_service
- Download strategies: download_page_images,
  download_iiif_manifest_and_images, try_pdf_first_then_images
- Manifest renderings: download_iiif_renderings
- Direct manifest flow: is_iiif_manifest_url, detect_provider_from_url,
  extract_item_id_from_url, extract_manifest_metadata, preview_manifest,
  download_from_iiif_manifest, is_direct_download_enabled,
  get_direct_link_column, get_naming_template, resolve_file_stem
"""
from __future__ import annotations

from ._direct import (
    IIIF_MANIFEST_PATTERNS,
    detect_provider_from_url,
    download_from_iiif_manifest,
    extract_item_id_from_url,
    extract_manifest_metadata,
    get_direct_link_column,
    get_naming_template,
    is_direct_download_enabled,
    is_iiif_manifest_url,
    preview_manifest,
    resolve_file_stem,
)
from ._parsing import (
    download_one_from_service,
    extract_direct_image_urls,
    extract_image_service_bases,
    image_url_candidates,
)
from ._renderings import download_iiif_renderings
from ._strategies import (
    download_iiif_manifest_and_images,
    download_page_images,
    try_pdf_first_then_images,
)

__all__ = [
    # Parsing
    "extract_image_service_bases",
    "extract_direct_image_urls",
    "image_url_candidates",
    "download_one_from_service",
    # Strategies
    "download_page_images",
    "download_iiif_manifest_and_images",
    "try_pdf_first_then_images",
    # Renderings
    "download_iiif_renderings",
    # Direct manifest flow
    "is_iiif_manifest_url",
    "detect_provider_from_url",
    "extract_item_id_from_url",
    "extract_manifest_metadata",
    "preview_manifest",
    "download_from_iiif_manifest",
    "is_direct_download_enabled",
    "get_direct_link_column",
    "get_naming_template",
    "resolve_file_stem",
    # Re-exported constants (tests and tools look them up)
    "IIIF_MANIFEST_PATTERNS",
]
